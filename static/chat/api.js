// All HTTP of the chat client. Every call sends X-Requested-With so the
// approuter answers an expired session with 401 (not a redirect fetch cannot
// follow); a 401 anywhere calls `handlers.onUnauthorized` once per page.
// SSE is read from the body of a fetch POST (EventSource cannot POST
// multipart), parsed by `createSSEParser` below.

export const BASE = '/chat/api';

export class ApiError extends Error {
  constructor(status, detail, code) {
    super(detail || `HTTP ${status}`);
    this.status = status;
    this.detail = detail || '';
    this.code = code || null;
  }
}

const handlers = { onUnauthorized: null };
export function setUnauthorizedHandler(fn) { handlers.onUnauthorized = fn; }

const fetchImpl = (...args) => globalThis.fetch(...args);

export function baseHeaders(extra = {}) {
  return { 'X-Requested-With': 'XMLHttpRequest', ...extra };
}

async function readDetail(res) {
  let detail = '';
  try {
    const text = await res.text();
    try {
      const j = JSON.parse(text);
      if (typeof j?.detail === 'string') detail = j.detail;
      else if (Array.isArray(j?.detail)) detail = j.detail.map((d) => d.msg || '').filter(Boolean).join('; ');
      else if (j?.message) detail = String(j.message);
    } catch { detail = text.slice(0, 200); }
  } catch { /* nothing to read */ }
  return detail;
}

function unauthorized(res) {
  if (res.status === 401 && typeof handlers.onUnauthorized === 'function') handlers.onUnauthorized();
}

/** JSON request. Returns parsed JSON, or null for 204. Throws ApiError. */
export async function request(path, { method = 'GET', body, headers, signal } = {}) {
  const init = { method, headers: baseHeaders(headers), signal, credentials: 'same-origin' };
  if (body instanceof FormData) init.body = body;
  else if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  const res = await fetchImpl(BASE + path, init);
  if (!res.ok) {
    unauthorized(res);
    throw new ApiError(res.status, await readDetail(res));
  }
  if (res.status === 204) return null;
  const ct = res.headers.get('content-type') || '';
  return ct.includes('json') ? res.json() : res.text();
}

export const getMe = () => request('/me');
export const listConversations = (q = '') =>
  request(`/conversations?limit=50${q ? `&q=${encodeURIComponent(q)}` : ''}`);
export const createConversation = (body = {}) => request('/conversations', { method: 'POST', body });
export const getConversation = (id) => request(`/conversations/${encodeURIComponent(id)}`);
export const patchConversation = (id, body) =>
  request(`/conversations/${encodeURIComponent(id)}`, { method: 'PATCH', body });
export const deleteConversation = (id) =>
  request(`/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' });
export const cancelRun = (id) =>
  request(`/conversations/${encodeURIComponent(id)}/cancel`, { method: 'POST' });

export const fileUrl = (id, inline = false) =>
  `${BASE}/files/${encodeURIComponent(id)}${inline ? '?inline=1' : ''}`;

/** Fetch a file as a Blob (for image thumbnails). */
export async function fetchFileBlob(id) {
  const res = await fetchImpl(fileUrl(id, true), { headers: baseHeaders(), credentials: 'same-origin' });
  if (!res.ok) {
    unauthorized(res);
    throw new ApiError(res.status, await readDetail(res));
  }
  return res.blob();
}

/**
 * Incremental text/event-stream parser. Feed it chunks (strings) in any
 * split; it calls `onEvent({type, data, raw})` for each dispatched event.
 * `data` is the JSON-parsed payload when it parses, else the raw string.
 * Comment lines (`: ping`) are dropped. Call `end()` for a trailing event
 * without a final blank line.
 */
export function createSSEParser(onEvent) {
  let buffer = '';
  let eventType = '';
  let dataLines = [];

  function dispatch() {
    if (!dataLines.length) { eventType = ''; return; }
    const raw = dataLines.join('\n');
    let data = raw;
    try { data = JSON.parse(raw); } catch { /* keep raw */ }
    const type = eventType || 'message';
    eventType = '';
    dataLines = [];
    onEvent({ type, data, raw });
  }

  function handleLine(line) {
    if (line === '') { dispatch(); return; }
    if (line.startsWith(':')) return;
    const colon = line.indexOf(':');
    let field = line;
    let value = '';
    if (colon >= 0) {
      field = line.slice(0, colon);
      value = line.slice(colon + 1);
      if (value.startsWith(' ')) value = value.slice(1);
    }
    if (field === 'event') eventType = value;
    else if (field === 'data') dataLines.push(value);
    // id / retry / unknown fields are ignored
  }

  return {
    push(chunk) {
      buffer += chunk;
      let nl;
      while ((nl = buffer.search(/\r\n|\n|\r/)) >= 0) {
        const sepLen = buffer[nl] === '\r' && buffer[nl + 1] === '\n' ? 2 : 1;
        // A lone CR at the very end could be half of CRLF: wait for more.
        if (buffer[nl] === '\r' && nl === buffer.length - 1) break;
        const line = buffer.slice(0, nl);
        buffer = buffer.slice(nl + sepLen);
        handleLine(line);
      }
    },
    end() {
      if (buffer) { handleLine(buffer); buffer = ''; }
      dispatch();
    },
  };
}

/**
 * POST a user turn and consume the SSE answer. `onEvent` gets every parsed
 * event; resolves when the stream ends (after `done`, or on a cut
 * connection). A non-2xx answer throws ApiError before any event.
 */
export async function sendMessage(conversationId, { text, files = [], model }, { onEvent, signal } = {}) {
  const form = new FormData();
  form.append('text', text);
  for (const f of files) form.append('files', f, f.name);
  if (model) form.append('model', model);
  const res = await fetchImpl(`${BASE}/conversations/${encodeURIComponent(conversationId)}/messages`, {
    method: 'POST',
    body: form,
    headers: baseHeaders({ Accept: 'text/event-stream' }),
    credentials: 'same-origin',
    signal,
  });
  if (!res.ok) {
    unauthorized(res);
    throw new ApiError(res.status, await readDetail(res));
  }
  await consumeStream(res.body, onEvent);
}

export async function consumeStream(body, onEvent) {
  const parser = createSSEParser(onEvent);
  const reader = body.getReader();
  const decoder = new TextDecoder('utf-8');
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    parser.push(decoder.decode(value, { stream: true }));
  }
  parser.push(decoder.decode());
  parser.end();
}
