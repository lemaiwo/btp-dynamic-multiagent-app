// Store + the reducer that applies SSE events to the in-flight assistant
// message. Pure: no DOM, no fetch (the jsdom tests import it as is).

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
  me: null,            // {principal, models, limits, app_name}
  conversations: [],   // sidebar list
  currentId: null,     // from the hash
  conversation: null,  // the open conversation with messages
  loading: false,
  run: null,           // {conversationId, assistantId, startedAt, controller}
  draftModel: null,    // model picked before a conversation exists
  banner: null,        // 'expired' | null
  sidebarOpen: false,  // the drawer on narrow screens
  query: '',
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
      const card = {
        type: 'tool',
        id: data.id,
        agent: data.agent ?? (i >= 0 ? parts[i].agent : undefined),
        name: data.name ?? (i >= 0 ? parts[i].name : undefined),
        status: data.status || 'running',
      };
      if (i >= 0) {
        const prev = parts[i];
        parts[i] = {
          ...prev, ...card,
          args: data.args !== undefined ? data.args : prev.args,
          output: data.output !== undefined ? data.output : prev.output,
        };
      } else {
        if (data.args !== undefined) card.args = data.args;
        if (data.output !== undefined) card.output = data.output;
        parts.push(card);
      }
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

/** Tool parts of a message, and how many are running / errored. */
export function activitySummary(message) {
  const parts = message?.parts || [];
  const tools = parts.filter((p) => p.type === 'tool');
  return {
    tools: tools.length,
    running: tools.filter((t) => t.status === 'running').length,
    errors: tools.filter((t) => t.status === 'error').length,
    notes: parts.filter((p) => p.type === 'note').length,
    hasActivity: tools.length > 0 || parts.some((p) => p.type === 'note' || p.type === 'message'),
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
    conv.messages[ai] = applyEvent(conv.messages[ai], event);
    if (event.type === 'done' || event.type === 'error') conv.status = 'idle';
  }
  return conv;
}
