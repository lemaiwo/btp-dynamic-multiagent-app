// Store + the reducer that applies SSE events to the in-flight assistant
// message, and the view model of the context drawer. Pure: no DOM, no fetch
// (the jsdom tests import it as is).

import { fillLevel, formatPct, formatSize, formatTokens } from './util.js';

/** A tiny observable store. `set` takes a patch object or an updater fn. */
export function createStore(initial) {
  let state = { ...initial };
  const listeners = new Set();
  return {
    get: () => state,
    set(patch) {
      const next = typeof patch === 'function' ? patch(state) : patch;
      if (!next) return;
      state = { ...state, ...next };
      for (const fn of listeners) fn(state);
    },
    subscribe(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
  };
}

export const initialState = {
  me: null,            // {principal, models, limits, app_name, developer, access_default, tiers}
  conversations: [],   // sidebar list
  currentId: null,     // from the hash
  conversation: null,  // the open conversation with messages
  loading: false,
  run: null,           // {conversationId, assistantId, startedAt, controller}
  draftModel: null,    // model picked before a conversation exists
  draftAccess: 'basic', // the switch's value for the next conversation ('basic' | 'advanced')
  catalogAccess: 'basic', // the access the loaded catalogue was asked for
  banner: null,        // 'expired' | null
  sidebarOpen: false,  // the drawer on narrow screens
  query: '',
  context: null,       // the last GET .../context answer of the open conversation (addendum e)
  contextError: false, // that GET failed (the drawer shows a fixed text)
};

let localSeq = 0;
export function localId(prefix = 'local') {
  localSeq += 1;
  return `${prefix}-${Date.now().toString(36)}-${localSeq}`;
}

export function newUserMessage(text, files = []) {
  return {
    id: localId('u'),
    role: 'user',
    content: text,
    parts: [],
    files,
    status: 'done',
    created_at: new Date().toISOString(),
  };
}

export function newAssistantMessage() {
  return {
    id: localId('a'),
    role: 'assistant',
    content: '',
    parts: [],
    files: [],
    status: 'running',
    note: null,
    error: null,
    created_at: new Date().toISOString(),
    started_at: Date.now(),
    duration_ms: null,
  };
}

function clone(msg) {
  return { ...msg, parts: [...(msg.parts || [])], files: [...(msg.files || [])] };
}

/**
 * Apply one SSE event to an assistant message; returns a NEW message object
 * (the old one is not mutated). Unknown events are ignored.
 */
export function applyEvent(message, event) {
  const type = event?.type;
  const data = event?.data || {};
  const msg = clone(message);
  const parts = msg.parts;
  switch (type) {
    case 'text': {
      const delta = String(data.delta ?? '');
      if (!delta) return message;
      const last = parts[parts.length - 1];
      if (last && last.type === 'text') parts[parts.length - 1] = { ...last, text: last.text + delta };
      else parts.push({ type: 'text', text: delta });
      msg.content = (msg.content || '') + delta;
      return msg;
    }
    case 'tool': {
      const i = parts.findIndex((p) => p.type === 'tool' && p.id === data.id);
      const prev = i >= 0 ? parts[i] : null;
      const card = {
        type: 'tool',
        id: data.id,
        agent: data.agent ?? prev?.agent,
        name: data.name ?? prev?.name,
        status: data.status || 'running',
        // the delegation the call ran under; null for the orchestrator's own
        agent_id: data.agent_id !== undefined ? (data.agent_id ?? null) : (prev?.agent_id ?? null),
      };
      if (prev) {
        parts[i] = {
          ...prev, ...card,
          args: data.args !== undefined ? data.args : prev.args,
          output: data.output !== undefined ? data.output : prev.output,
          duration_ms: data.duration_ms != null ? Number(data.duration_ms) : prev.duration_ms,
        };
      } else {
        if (data.args !== undefined) card.args = data.args;
        if (data.output !== undefined) card.output = data.output;
        if (data.duration_ms != null) card.duration_ms = Number(data.duration_ms);
        parts.push(card);
      }
      return msg;
    }
    case 'agent': {
      // One delegation: "running" carries the prompt, the closing frame the
      // output, duration and usage. Merged by id into one part.
      const i = parts.findIndex((p) => p.type === 'agent' && p.id === data.id);
      const prev = i >= 0 ? parts[i] : null;
      const part = {
        type: 'agent',
        id: data.id,
        agent: data.agent ?? prev?.agent,
        parent: data.parent !== undefined ? (data.parent ?? null) : (prev?.parent ?? null),
        status: data.status || 'running',
        prompt: data.prompt !== undefined ? data.prompt : prev?.prompt,
        output: data.output !== undefined ? data.output : prev?.output,
        duration_ms: data.duration_ms != null ? Number(data.duration_ms) : prev?.duration_ms,
        usage: data.usage !== undefined ? data.usage : prev?.usage,
      };
      for (const k of ['prompt', 'output', 'duration_ms', 'usage']) if (part[k] === undefined) delete part[k];
      if (prev) parts[i] = { ...prev, ...part };
      else parts.push(part);
      return msg;
    }
    case 'usage': {
      // Once per turn, before `done`; a repeat replaces the first.
      const part = { ...data, type: 'usage' };
      const i = parts.findIndex((p) => p.type === 'usage');
      if (i >= 0) parts[i] = part;
      else parts.push(part);
      return msg;
    }
    case 'note': {
      const text = String(data.text ?? '');
      msg.note = text || null;
      const last = parts[parts.length - 1];
      if (last && last.type === 'note') parts[parts.length - 1] = { ...last, text };
      else if (text) parts.push({ type: 'note', text });
      return msg;
    }
    case 'message': {
      parts.push({ type: 'message', text: String(data.text ?? '') });
      return msg;
    }
    case 'file': {
      if (parts.some((p) => p.type === 'file' && p.file_id === data.file_id)) return message;
      parts.push({
        type: 'file',
        file_id: data.file_id,
        name: data.name,
        media_type: data.media_type,
        size: data.size,
        // a dashboard's file event carries its title and widget count (addendum i)
        ...(typeof data.title === 'string' ? { title: data.title } : {}),
        ...(Number.isInteger(data.widgets) ? { widgets: data.widgets } : {}),
      });
      return msg;
    }
    case 'error': {
      msg.error = { code: data.code || 'run_failed', message: data.message || '' };
      msg.status = data.code === 'canceled' ? 'canceled' : 'failed';
      msg.note = null;
      return msg;
    }
    case 'done': {
      if (data.message_id) msg.server_id = data.message_id;
      msg.status = data.status || 'done';
      msg.note = null;
      if (msg.started_at && msg.duration_ms == null) msg.duration_ms = Date.now() - msg.started_at;
      if (data.duration_ms != null) msg.duration_ms = Number(data.duration_ms);
      return msg;
    }
    default:
      return message;
  }
}

/** The `usage` part of a message, or null. */
export function usagePart(message) {
  return (message?.parts || []).find((p) => p.type === 'usage') || null;
}

function tokensOf(usage) {
  if (!usage) return 0;
  const total = Number(usage.total_tokens);
  if (Number.isFinite(total) && total > 0) return total;
  return (Number(usage.input_tokens) || 0) + (Number(usage.output_tokens) || 0);
}

/**
 * What the activity strip's headline says: agents, tools, how many are
 * running / errored, the tokens (the usage part when there is one, else the
 * sum over the agent parts) and the duration (live: now - started_at;
 * stored: the usage part's duration_ms, else the message's).
 */
export function activitySummary(message, now = Date.now()) {
  const parts = message?.parts || [];
  const tools = parts.filter((p) => p.type === 'tool');
  const agents = parts.filter((p) => p.type === 'agent');
  const usage = usagePart(message);
  const tokens = usage ? tokensOf(usage) : agents.reduce((n, a) => n + tokensOf(a.usage), 0);
  let duration = null;
  if (message?.status === 'running') duration = message.started_at ? Math.max(0, now - message.started_at) : null;
  else if (usage && usage.duration_ms != null) duration = Number(usage.duration_ms);
  else if (message?.duration_ms != null) duration = Number(message.duration_ms);
  return {
    tools: tools.length,
    agents: agents.length,
    running: tools.filter((t) => t.status === 'running').length + agents.filter((a) => a.status === 'running').length,
    errors: tools.filter((t) => t.status === 'error').length + agents.filter((a) => a.status === 'error').length,
    notes: parts.filter((p) => p.type === 'note').length,
    tokens,
    duration,
    hasActivity: tools.length > 0 || agents.length > 0 || parts.some((p) => p.type === 'note' || p.type === 'message'),
  };
}

/**
 * The activity tree of a message, in order of first appearance:
 * top-level nodes are the orchestrator's own tools (`agent_id` null), the
 * delegations nobody delegated (`parent` null) and the notes / notices;
 * an agent node's `children` are the tools with its id as `agent_id` and the
 * delegations with its id as `parent`. A tool or delegation whose owner is
 * not a known agent part is shown at the top level rather than lost.
 * -> [{kind:'tool'|'agent'|'note'|'message', part, children?}]
 */
export function activityTree(message) {
  const parts = message?.parts || [];
  const agentIds = new Set(parts.filter((p) => p.type === 'agent' && p.id != null).map((p) => p.id));
  const nodes = new Map(); // agent id -> node
  const top = [];
  const nodeOf = (part) => {
    if (part.type === 'agent') {
      const node = { kind: 'agent', part, children: [] };
      if (part.id != null && !nodes.has(part.id)) nodes.set(part.id, node);
      return node;
    }
    return { kind: part.type, part };
  };
  const pending = [];
  for (const part of parts) {
    if (part.type !== 'tool' && part.type !== 'agent' && part.type !== 'note' && part.type !== 'message') continue;
    const node = nodeOf(part);
    const owner = part.type === 'tool' ? part.agent_id : part.type === 'agent' ? part.parent : null;
    if (owner != null && agentIds.has(owner) && owner !== part.id) pending.push([owner, node]);
    else top.push(node);
  }
  // Attach children after every agent node exists (a tool frame can arrive
  // before its delegation's part in a stored list that was reordered).
  for (const [owner, node] of pending) {
    const parent = nodes.get(owner);
    if (parent && !isAncestor(node, parent)) parent.children.push(node);
    else top.push(node);
  }
  return top;
}

function isAncestor(node, candidate) {
  // true when `candidate` is inside `node` (guards a parent cycle)
  if (node.kind !== 'agent') return false;
  const stack = [...node.children];
  while (stack.length) {
    const n = stack.pop();
    if (n === candidate) return true;
    if (n.kind === 'agent') stack.push(...n.children);
  }
  return false;
}

function subtractUsage(total, usage) {
  const t = total || {};
  const u = usage || {};
  const num = (v) => (Number.isFinite(Number(v)) ? Number(v) : 0);
  const own = Number(u.total_tokens);
  return {
    input_tokens: num(t.input_tokens) - num(u.input_tokens),
    output_tokens: num(t.output_tokens) - num(u.output_tokens),
    total_tokens: num(t.total_tokens) - (Number.isFinite(own) && own > 0 ? own : num(u.input_tokens) + num(u.output_tokens)),
    requests: num(t.requests) - num(u.requests),
    turns: Math.max(0, num(t.turns) - 1),
  };
}

/** The conversation total after one more turn's usage part. */
export function addUsage(total, usage) {
  const t = total || {};
  const u = usage || {};
  const num = (v) => (Number.isFinite(Number(v)) ? Number(v) : 0);
  const input = num(t.input_tokens) + num(u.input_tokens);
  const output = num(t.output_tokens) + num(u.output_tokens);
  const own = Number(u.total_tokens);
  return {
    input_tokens: input,
    output_tokens: output,
    total_tokens: num(t.total_tokens) + (Number.isFinite(own) && own > 0 ? own : num(u.input_tokens) + num(u.output_tokens)),
    requests: num(t.requests) + num(u.requests),
    turns: num(t.turns) + 1,
  };
}

/**
 * Apply a stream event at the conversation level: `start` binds the real ids
 * and the stored upload list to the two local messages, `title` renames the
 * conversation, everything else goes to the assistant message.
 * Returns a NEW conversation object.
 */
export function applyStreamEvent(conversation, userLocalId, assistantLocalId, event) {
  const conv = { ...conversation, messages: [...(conversation.messages || [])] };
  const ui = conv.messages.findIndex((m) => m.id === userLocalId);
  const ai = conv.messages.findIndex((m) => m.id === assistantLocalId);
  if (event.type === 'start') {
    const d = event.data || {};
    if (ui >= 0) {
      conv.messages[ui] = {
        ...conv.messages[ui],
        server_id: d.user_message_id,
        files: Array.isArray(d.files) ? d.files.map((f) => ({ ...f, kind: 'upload' })) : conv.messages[ui].files,
      };
    }
    if (ai >= 0) conv.messages[ai] = { ...conv.messages[ai], server_id: d.assistant_message_id };
    conv.status = 'running';
    return conv;
  }
  if (event.type === 'title') {
    conv.title = String(event.data?.title || conv.title);
    return conv;
  }
  if (ai >= 0) {
    const before = usagePart(conv.messages[ai]);
    conv.messages[ai] = applyEvent(conv.messages[ai], event);
    if (event.type === 'usage') {
      // the header chip: count this turn once, even when the event repeats
      const after = usagePart(conv.messages[ai]);
      let total = conv.usage || null;
      if (before) total = subtractUsage(total, before);
      conv.usage = addUsage(total, after);
    }
    if (event.type === 'done' || event.type === 'error') conv.status = 'idle';
  }
  return conv;
}

// ---------- the context drawer (addendum e) ----------
const KIND_LABELS = { orchestrator: 'Orchestrator', admin: 'Admin agent', mine: 'Personal agent' };
const TIER_LABELS = { basic: 'Basic', advanced: 'Advanced' };
const TOOL_GROUPS = [
  ['document', 'Document tools'],
  ['deep', 'Deep agent tools'],
  ['delegate', 'Delegation'],
  ['connector', 'Connectors (shared by admin agents)'],
];

const str = (v, fallback = '') => (typeof v === 'string' ? v : fallback);
const int = (v) => { const n = Number(v); return Number.isFinite(n) && n >= 0 ? Math.round(n) : 0; };
const plural = (n, one, many) => `${n.toLocaleString('en-US')} ${n === 1 ? one : many}`;

/** The file ids the context answer marks excluded (for the struck-through cards). */
export function excludedFileIds(context) {
  const out = new Set();
  for (const a of context?.history?.attachments || []) {
    if (a && a.excluded === true && a.file_id != null) out.add(String(a.file_id));
  }
  return out;
}

/**
 * What the drawer shows, from a GET .../context answer: five sections of
 * plain strings and rows. Every field is tolerant of a missing or odd
 * value; nothing here is markup, the renderer inserts text nodes only.
 */
export function contextViewModel(ctx) {
  const c = ctx && typeof ctx === 'object' ? ctx : {};
  const agent = c.agent && typeof c.agent === 'object' ? c.agent : {};
  const model = c.model && typeof c.model === 'object' ? c.model : {};
  const instr = c.instructions && typeof c.instructions === 'object' ? c.instructions : {};
  const hist = c.history && typeof c.history === 'object' ? c.history : {};
  const usage = c.usage && typeof c.usage === 'object' ? c.usage : {};
  const windowTokens = Number(model.context_window);
  const hasWindow = Number.isFinite(windowTokens) && windowTokens > 0;
  const fill = fillLevel(hist.estimated_tokens, hist.measured_tokens, hasWindow ? windowTokens : null);
  const kind = str(agent.kind);
  const tier = str(agent.tier);
  const access = str(c.access);
  const who = {
    agent: str(agent.name) || (kind === 'orchestrator' ? 'Orchestrator' : 'Agent'),
    kind: KIND_LABELS[kind] || '',
    tier: TIER_LABELS[tier] || tier,
    access: TIER_LABELS[access] || access,
    model: str(model.id) || 'Default model',
    window: hasWindow ? windowTokens : null,
    fill,
    pctText: formatPct(fill.pct),
    fillText: hasWindow
      ? `~${formatTokens(fill.tokens)} of ${formatTokens(windowTokens)} tokens`
      : `~${formatTokens(fill.tokens)} tokens · window unknown`,
    fillSource: fill.measured ? 'measured on the last request' : 'estimated',
  };
  const chars = int(instr.chars);
  const visible = instr.visible === true && typeof instr.text === 'string';
  const skills = (Array.isArray(instr.skills) ? instr.skills : []).filter((s) => s && typeof s === 'object').map((s) => {
    const own = str(s.scope) === 'mine';
    const text = s.visible === true && typeof s.text === 'string' ? s.text : null;
    return {
      name: str(s.name) || 'skill',
      scope: own ? 'mine' : 'admin',
      scopeLabel: own ? 'Mine' : 'Admin',
      chars: int(s.chars),
      charsText: plural(int(s.chars), 'character', 'characters'),
      text,
    };
  });
  const instructions = {
    visible,
    text: visible ? instr.text : null,
    chars,
    hiddenText: `${plural(chars, 'character', 'characters')} · shown to admins only`,
    skills,
  };
  const attachments = (Array.isArray(hist.attachments) ? hist.attachments : []).filter((a) => a && typeof a === 'object' && a.file_id != null).map((a) => ({
    file_id: String(a.file_id),
    name: str(a.name) || 'file',
    media_type: str(a.media_type),
    size: int(a.size),
    sizeText: formatSize(int(a.size)),
    excluded: a.excluded === true,
  }));
  const measured = hist.measured_tokens == null ? NaN : Number(hist.measured_tokens);
  const history = {
    turns: int(hist.turns),
    messages: int(hist.messages),
    estimated: int(hist.estimated_tokens),
    measured: Number.isFinite(measured) && measured >= 0 ? Math.round(measured) : null,
    summary: `${plural(int(hist.turns), 'turn', 'turns')} · ${plural(int(hist.messages), 'message', 'messages')}`,
    estimatedText: `~${formatTokens(int(hist.estimated_tokens))} tokens estimated`,
    measuredText: Number.isFinite(measured) && measured >= 0 ? `${formatTokens(measured)} tokens measured on the last request` : 'not measured yet',
    attachments,
  };
  const byKind = new Map(TOOL_GROUPS.map(([k]) => [k, []]));
  for (const t of Array.isArray(c.tools) ? c.tools : []) {
    if (!t || typeof t !== 'object') continue;
    const k = byKind.has(t.kind) ? t.kind : 'document';
    const name = str(t.name) || 'tool';
    const target = str(t.agent);
    byKind.get(k).push({ kind: k, name, agent: target || null, label: k === 'delegate' && target ? `${name} → ${target}` : name });
  }
  const tools = TOOL_GROUPS.map(([kind, label]) => ({ kind, label, items: byKind.get(kind) })).filter((g) => g.items.length);
  const total = int(usage.total_tokens);
  const usageOut = {
    total,
    totalText: `${formatTokens(total)} tokens`,
    detail: `${formatTokens(int(usage.input_tokens))} in / ${formatTokens(int(usage.output_tokens))} out · ${plural(int(usage.requests), 'request', 'requests')} over ${plural(int(usage.turns), 'turn', 'turns')}`,
  };
  return { who, instructions, history, tools, usage: usageOut };
}
