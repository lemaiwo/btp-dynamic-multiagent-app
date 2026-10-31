// The message column: user turns as tinted bubbles on the right, assistant
// turns flowing in the column (activity strip, markdown, file cards, error).
// Nodes are keyed by message id and only rebuilt when their content changed,
// so a streaming delta touches one message and nothing above it moves.

import { renderMarkdown } from './markdown.js';
import { fileCards } from './files.js';
import { activityStrip } from './activity.js';
import { usagePart } from '../state.js';
import { formatElapsed, formatTokens } from '../util.js';

function textOf(message) {
  if (message.content) return message.content;
  return (message.parts || []).filter((p) => p.type === 'text').map((p) => p.text).join('');
}

function generatedFiles(message) {
  const seen = new Set();
  const out = [];
  for (const p of message.parts || []) {
    if (p.type === 'file' && !seen.has(p.file_id)) {
      seen.add(p.file_id);
      out.push({ id: p.file_id, name: p.name, media_type: p.media_type, size: p.size });
    }
  }
  for (const f of message.files || []) {
    if ((f.kind === 'generated' || !f.kind) && !seen.has(f.id)) {
      seen.add(f.id);
      out.push(f);
    }
  }
  return out;
}

const ERROR_TEXTS = {
  run_failed: 'The run failed.',
  canceled: 'Stopped.',
  model_unavailable: 'The model is not available right now.',
  credential_required: 'A sign-in is needed before the agents can continue.',
  agent_unavailable: 'The selected agent is no longer available. Pick another one in the header.',
};

function buildUser(message) {
  const art = document.createElement('article');
  art.className = 'msg user';
  art.dataset.id = message.id;
  art.setAttribute('aria-label', 'You');
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.textContent = message.content || '';
  art.appendChild(bubble);
  if (message.files && message.files.length) art.appendChild(fileCards(message.files));
  return art;
}

function buildAssistant() {
  const art = document.createElement('article');
  art.className = 'msg assistant';
  art.setAttribute('aria-label', 'Agents');
  const from = document.createElement('div');
  from.className = 'from';
  from.textContent = 'Agents';
  art.appendChild(from);
  const activity = document.createElement('div');
  activity.className = 'activity-slot';
  const body = document.createElement('div');
  body.className = 'md body';
  const files = document.createElement('div');
  files.className = 'files-slot';
  const error = document.createElement('div');
  error.className = 'error-slot';
  const usage = document.createElement('div');
  usage.className = 'usage-slot';
  art.append(activity, body, files, error, usage);
  return art;
}

function partSig(p) {
  switch (p.type) {
    case 'text': return 't';
    case 'tool': return [p.id, p.status, !!p.output, p.duration_ms, p.agent_id];
    case 'agent': return [p.id, p.status, !!p.output, p.duration_ms, p.usage?.total_tokens];
    case 'usage': return ['usage', p.total_tokens, p.duration_ms];
    default: return [p.type, p.text];
  }
}

function activitySig(message) {
  return JSON.stringify([message.status, message.note, message.duration_ms, (message.parts || []).map(partSig)]);
}

/**
 * "gpt-4o · 1,240 in / 380 out · 8.1 s" under an assistant message, with the
 * per-agent split as the tooltip ("btp-admin 820 in / 120 out (2 requests)").
 */
export function usageLine(usage) {
  if (!usage) return null;
  const el = document.createElement('div');
  el.className = 'usage-line';
  const bits = [];
  if (usage.model) bits.push(String(usage.model));
  bits.push(`${formatTokens(usage.input_tokens || 0)} in / ${formatTokens(usage.output_tokens || 0)} out`);
  if (usage.duration_ms != null) bits.push(formatElapsed(usage.duration_ms));
  el.textContent = bits.join(' · ');
  const agents = Array.isArray(usage.agents) ? usage.agents : [];
  const lines = agents.map((a) => {
    const n = Number(a.requests) || 0;
    const req = n ? ` (${n === 1 ? '1 request' : `${n} requests`})` : '';
    return `${a.agent || a.id || 'agent'} ${formatTokens(a.input_tokens || 0)} in / ${formatTokens(a.output_tokens || 0)} out${req}`;
  });
  const total = `Total ${formatTokens(usage.total_tokens || 0)} tokens` + (usage.requests ? ` · ${usage.requests} requests` : '') + (usage.cache_read_tokens ? ` · ${formatTokens(usage.cache_read_tokens)} cached` : '');
  el.title = [...lines, total].join('\n');
  el.setAttribute('aria-label', `Usage: ${el.textContent}. ${lines.join('; ')}`);
  return el;
}

function updateAssistant(el, message, live, agentName = 'Agents') {
  el.dataset.id = message.id;
  const running = message.status === 'running';
  el.classList.toggle('running', running);
  // who answers: the conversation's agent_name, "Agents" when unknown
  const from = el.querySelector('.from');
  if (from && from.textContent !== agentName) {
    from.textContent = agentName;
    el.setAttribute('aria-label', agentName);
  }

  const asig = activitySig(message);
  if (el._asig !== asig) {
    el._asig = asig;
    const slot = el.querySelector('.activity-slot');
    const prev = slot.querySelector('details');
    const strip = activityStrip(message, { live, previous: prev });
    slot.replaceChildren(...(strip ? [strip] : []));
  }

  const text = textOf(message);
  const bsig = `${message.status}|${text.length}|${text.slice(-64)}`;
  const body = el.querySelector('.body');
  if (el._bsig !== bsig) {
    el._bsig = bsig;
    if (text) renderMarkdown(body, text, { final: !running });
    else body.replaceChildren();
    body.classList.toggle('streaming', running && !!text);
    body.hidden = !text;
  }

  const files = generatedFiles(message);
  const fsig = files.map((f) => f.id).join(',');
  if (el._fsig !== fsig) {
    el._fsig = fsig;
    const slot = el.querySelector('.files-slot');
    slot.replaceChildren(...(files.length ? [fileCards(files)] : []));
  }

  const esig = JSON.stringify([message.status, message.error]);
  if (el._esig !== esig) {
    el._esig = esig;
    const slot = el.querySelector('.error-slot');
    slot.replaceChildren();
    if (message.status === 'failed' || (message.status === 'canceled' && !text)) {
      const box = document.createElement('div');
      box.className = `callout ${message.status}`;
      box.setAttribute('role', message.status === 'failed' ? 'alert' : 'status');
      const code = message.error?.code || (message.status === 'canceled' ? 'canceled' : 'run_failed');
      const head = document.createElement('strong');
      head.textContent = ERROR_TEXTS[code] || ERROR_TEXTS.run_failed;
      box.appendChild(head);
      if (message.error?.message && code !== 'canceled') {
        const p = document.createElement('span');
        p.textContent = ` ${message.error.message}`;
        box.appendChild(p);
      }
      slot.appendChild(box);
    }
  }

  const usage = usagePart(message);
  const usig = JSON.stringify(usage);
  if (el._usig !== usig) {
    el._usig = usig;
    const slot = el.querySelector('.usage-slot');
    const line = usageLine(usage);
    slot.replaceChildren(...(line ? [line] : []));
    slot.hidden = !line;
  }
}

export function createMessageList(container) {
  const nodes = new Map();
  // Follow the stream while the user is at the bottom; stop following when
  // they scroll up. Scroll anchoring moves scrollTop *down* when content
  // above grows (a mermaid fence becoming a diagram), so only a decrease of
  // scrollTop counts as the user leaving the bottom.
  let stick = true;
  let lastTop = 0;
  const scroller = container.closest('.log') || container.parentElement;
  scroller.addEventListener('scroll', () => {
    const top = scroller.scrollTop;
    const atBottom = scroller.scrollHeight - top - scroller.clientHeight < 96;
    if (top < lastTop - 2) stick = atBottom;
    else if (atBottom) stick = true;
    lastTop = top;
  });

  function scrollToBottom(force = false) {
    if (force || stick) {
      scroller.scrollTop = scroller.scrollHeight;
      lastTop = scroller.scrollTop;
    }
  }
  container.addEventListener('chat:layout', () => scrollToBottom());

  function render(conversation, { live = false, agentName = 'Agents' } = {}) {
    const messages = conversation?.messages || [];
    if (container.classList.contains('empty-state')) {
      // the empty state gives way to the first message
      container.classList.remove('empty-state');
      container.replaceChildren();
      nodes.clear();
    }
    const keep = new Set();
    let cursor = null;
    for (const m of messages) {
      keep.add(m.id);
      let el = nodes.get(m.id);
      if (!el) {
        el = m.role === 'user' ? buildUser(m) : buildAssistant();
        nodes.set(m.id, el);
      }
      if (m.role === 'assistant') updateAssistant(el, m, live, agentName || 'Agents');
      const next = cursor ? cursor.nextSibling : container.firstChild;
      if (next !== el) container.insertBefore(el, next);
      cursor = el;
    }
    for (const [id, el] of nodes) {
      if (!keep.has(id)) { el.remove(); nodes.delete(id); }
    }
    scrollToBottom();
  }

  function clear() {
    nodes.clear();
    container.replaceChildren();
    stick = true;
  }

  return { render, clear, scrollToBottom, resetStick: () => { stick = true; } };
}

const PROMPTS = [
  'Which BTP subaccounts have entitlements that nobody uses?',
  'Summarise the open pull requests on our Fiori repositories',
  'Create an Excel workbook of the destinations in our dev subaccount',
];

/**
 * The empty state: logo, greeting, three example prompts. With a picked
 * agent the heading names it and the subline is its description.
 */
export function renderEmpty(container, { appName, onPrompt, agentName = '', agentDescription = '' }) {
  container.classList.add('empty-state');
  const wrap = document.createElement('div');
  wrap.className = 'empty';
  const logo = document.createElement('img');
  logo.src = '/static/sap.png';
  logo.alt = '';
  logo.className = 'empty-logo';
  const h = document.createElement('h2');
  h.className = 'greeting';
  h.textContent = agentName ? `Ask ${agentName}` : 'What are we doing on BTP today?';
  const sub = document.createElement('p');
  sub.className = 'greeting-sub';
  sub.textContent = agentName
    ? (agentDescription || `This conversation goes straight to ${agentName}. Attach a document or ask for one.`)
    : `${appName || 'SAP BTP Multi-Agent'} hands your question to the specialist that can answer it. Attach a document or ask for one.`;
  const chips = document.createElement('div');
  chips.className = 'prompt-chips';
  for (const text of PROMPTS) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'prompt-chip';
    b.textContent = text;
    b.addEventListener('click', () => onPrompt(text));
    chips.appendChild(b);
  }
  wrap.append(logo, h, sub, chips);
  container.replaceChildren(wrap);
}
