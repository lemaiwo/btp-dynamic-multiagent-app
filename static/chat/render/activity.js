// The activity strip of an assistant message: "Worked for 8.1 s · 2 agents ·
// 5 tools · 3.2k tokens", open while running (with the live note), collapsed
// when done. Inside, the activity TREE: the orchestrator's own tool rows at
// the top level, one card per delegation (status dot, name, duration,
// tokens) that expands to the prompt it received, its tool rows and its
// answer; a nested delegation one level deeper behind an indent guide. Tool
// rows expand to the args / output previews with their duration.
//
// Everything here is text (textContent); the one notice that is markdown
// (`message` parts, the sign-in link) goes through toSafeHtml.

import { formatElapsed, formatTokens } from '../util.js';
import { activitySummary, activityTree, usagePart } from '../state.js';
import { toSafeHtml } from './markdown.js';

const PREVIEW_MAX = 400;

function count(n, one, many) {
  return n === 1 ? `1 ${one}` : `${n} ${many}`;
}

function headline(message, summary) {
  const agentsText = summary.agents ? count(summary.agents, 'agent', 'agents') : '';
  const toolsText = summary.tools ? count(summary.tools, 'tool', 'tools') : '';
  const tokensText = summary.tokens ? `${formatTokens(summary.tokens)} tokens` : '';
  if (message.status === 'running') {
    const note = message.note || (summary.running ? 'Running tools…' : '');
    return ['Working…', note, agentsText, toolsText].filter(Boolean).join(' · ');
  }
  const counts = [agentsText, toolsText, tokensText].filter(Boolean);
  if (message.status === 'canceled') return ['Stopped', ...counts].join(' · ');
  const worked = summary.duration != null ? `Worked for ${formatElapsed(summary.duration)}` : 'Worked';
  return [worked, ...counts].join(' · ');
}

function preview(text) {
  const s = String(text ?? '').trim();
  return s.length > PREVIEW_MAX ? `${s.slice(0, PREVIEW_MAX - 1)}…` : s;
}

function tokensOf(usage) {
  if (!usage) return 0;
  const t = Number(usage.total_tokens);
  if (Number.isFinite(t) && t > 0) return t;
  return (Number(usage.input_tokens) || 0) + (Number(usage.output_tokens) || 0);
}

function dotFor(status) {
  const d = document.createElement('span');
  d.className = `dot ${status === 'running' ? 'running' : status === 'error' ? 'error' : 'done'}`;
  d.setAttribute('aria-hidden', 'true');
  return d;
}

function statusWord(status) {
  return status === 'running' ? 'running' : status === 'error' ? 'failed' : 'done';
}

/** The right-hand meta of a row: duration and tokens, tabular, fixed slot. */
function meta(part, { tokens = false } = {}) {
  const m = document.createElement('span');
  m.className = 'act-meta';
  const bits = [];
  if (part.status === 'running') bits.push('running');
  else {
    if (part.duration_ms != null) bits.push(formatElapsed(part.duration_ms));
    if (tokens && tokensOf(part.usage)) bits.push(`${formatTokens(tokensOf(part.usage))} tok`);
    if (part.status === 'error') bits.push('failed');
    if (!bits.length) bits.push(statusWord(part.status));
  }
  m.textContent = bits.join(' · ');
  return m;
}

function block(label, text, cls) {
  const wrap = document.createElement('div');
  wrap.className = `act-block ${cls}`;
  const l = document.createElement('span');
  l.className = 'act-block-label';
  l.textContent = label;
  const body = document.createElement('div');
  body.className = 'act-block-text';
  body.textContent = text;
  wrap.append(l, body);
  return wrap;
}

function chevron() {
  const chev = document.createElement('span');
  chev.className = 'chev';
  chev.setAttribute('aria-hidden', 'true');
  return chev;
}

/**
 * `<details>` whose open state survives a rebuild: `known` maps the keys of
 * the previous strip to their open state. A card that is new opens when
 * `openByDefault` (a running delegation, so its tool rows are seen live);
 * once it exists it keeps whatever state it has, so a card flipping from
 * running to done does not fold under the reader.
 */
function foldable(key, cls, known, openByDefault = false) {
  const d = document.createElement('details');
  d.className = cls;
  d.dataset.key = key;
  d.open = known.has(key) ? known.get(key) : openByDefault;
  return d;
}

function toolRow(part, { openKeys, ownerName }) {
  const key = `tool:${part.id}`;
  const args = preview(part.args);
  const out = preview(part.output);
  const expandable = !!(args || out);
  const li = document.createElement('li');
  li.className = `tool status-${part.status || 'running'}`;

  const head = document.createElement(expandable ? 'summary' : 'div');
  head.className = 'tool-head';
  const label = document.createElement('span');
  label.className = 'tool-label';
  // A top-level row names its agent when the part says one (an older stored
  // part, or a run whose delegation frame was lost); under a card the name
  // is the card's.
  if (part.agent && part.agent !== ownerName) {
    const agent = document.createElement('span');
    agent.className = 'tool-agent';
    agent.textContent = part.agent;
    label.append(agent, document.createTextNode(' '));
  }
  const name = document.createElement('span');
  name.className = 'tool-name';
  name.textContent = part.name || 'tool';
  label.appendChild(name);
  head.append(dotFor(part.status), label, meta(part));

  if (!expandable) {
    li.appendChild(head);
    return li;
  }
  head.appendChild(chevron());
  const d = foldable(key, 'tool-fold', openKeys);
  d.appendChild(head);
  const pre = document.createElement('div');
  pre.className = 'tool-preview';
  if (args) {
    const a = document.createElement('code');
    a.textContent = args;
    pre.appendChild(a);
  }
  if (out) {
    const o = document.createElement('div');
    o.className = 'tool-output';
    o.textContent = out;
    pre.appendChild(o);
  }
  d.appendChild(pre);
  li.appendChild(d);
  return li;
}

function agentCard(node, { openKeys, depth }) {
  const part = node.part;
  const key = `agent:${part.id}`;
  const li = document.createElement('li');
  li.className = `agent status-${part.status || 'running'} depth-${Math.min(depth, 2)}`;
  const d = foldable(key, 'agent-fold', openKeys, part.status === 'running');
  const head = document.createElement('summary');
  head.className = 'agent-head';
  const name = document.createElement('span');
  name.className = 'agent-name';
  name.textContent = part.agent || 'agent';
  head.append(dotFor(part.status), name, meta(part, { tokens: true }), chevron());
  d.appendChild(head);

  const body = document.createElement('div');
  body.className = 'agent-body';
  const prompt = preview(part.prompt);
  if (prompt) body.appendChild(block('Asked', prompt, 'asked'));
  if (node.children.length) body.appendChild(list(node.children, { openKeys, depth: depth + 1, ownerName: part.agent }));
  const out = preview(part.output);
  if (out) body.appendChild(block(part.status === 'error' ? 'Failed' : 'Answered', out, part.status === 'error' ? 'failed' : 'answered'));
  else if (part.status === 'running') body.appendChild(block('Answered', '…', 'answered pending'));
  d.appendChild(body);
  li.appendChild(d);
  return li;
}

function list(nodes, ctx) {
  const ul = document.createElement('ul');
  ul.className = `activity-list${ctx.depth ? ' nested' : ''}`;
  for (const node of nodes) {
    if (node.kind === 'tool') ul.appendChild(toolRow(node.part, ctx));
    else if (node.kind === 'agent') ul.appendChild(agentCard(node, ctx));
    else if (node.kind === 'note') {
      const li = document.createElement('li');
      li.className = 'note';
      li.textContent = node.part.text;
      ul.appendChild(li);
    } else if (node.kind === 'message') {
      const li = document.createElement('li');
      li.className = 'notice md';
      li.innerHTML = toSafeHtml(node.part.text);
      ul.appendChild(li);
    }
  }
  return ul;
}

/** "gpt-4o · 1,240 in / 380 out · 3 requests" for the strip's footer. */
function usageFooter(usage) {
  const li = document.createElement('li');
  li.className = 'usage-total';
  const bits = [];
  if (usage.model) bits.push(String(usage.model));
  bits.push(`${formatTokens(usage.input_tokens || 0)} in / ${formatTokens(usage.output_tokens || 0)} out`);
  if (usage.cache_read_tokens) bits.push(`${formatTokens(usage.cache_read_tokens)} cached`);
  if (usage.requests) bits.push(count(Number(usage.requests), 'request', 'requests'));
  li.textContent = `Total · ${bits.join(' · ')}`;
  return li;
}

/** Build (or rebuild) the strip. Open states are kept across rebuilds. */
export function activityStrip(message, { live = false, previous = null } = {}) {
  const summary = activitySummary(message);
  if (!summary.hasActivity && message.status !== 'running') return null;
  const running = message.status === 'running';
  const details = document.createElement('details');
  details.className = `activity status-${message.status}`;
  // Open while running, collapsed once done, unless the user opened or
  // closed it by hand (a click on the summary; the `toggle` event also fires
  // for a change made by script, so it cannot tell the two apart).
  const userToggled = !!(previous && previous.dataset.userToggled);
  details.open = userToggled ? previous.open : running;
  if (userToggled) details.dataset.userToggled = '1';
  const openKeys = new Map(
    previous ? [...previous.querySelectorAll('details[data-key]')].map((d) => [d.dataset.key, d.open]) : [],
  );

  const sum = document.createElement('summary');
  sum.className = 'activity-summary';
  sum.addEventListener('click', () => { details.dataset.userToggled = '1'; });
  const dot = document.createElement('span');
  dot.className = `dot ${running ? 'running' : summary.errors || message.status === 'failed' ? 'error' : message.status === 'canceled' ? 'neutral' : 'done'}`;
  dot.setAttribute('aria-hidden', 'true');
  const text = document.createElement('span');
  text.className = 'activity-headline';
  text.textContent = headline(message, summary, live);
  sum.append(dot, text, chevron());
  details.appendChild(sum);

  // Heartbeat notes are phase labels between tool rows in a flat list; in a
  // tree they would trail below the cards, and the live one is in the
  // headline, so they are shown only while a message has no agent parts.
  const nodes = activityTree(message);
  const hasAgents = nodes.some((n) => n.kind === 'agent');
  const tree = list(hasAgents ? nodes.filter((n) => n.kind !== 'note') : nodes, { openKeys, depth: 0, ownerName: null });
  tree.classList.add('activity-tree');
  const usage = usagePart(message);
  if (usage) tree.appendChild(usageFooter(usage));
  details.appendChild(tree);
  return details;
}
