// The activity strip of an assistant message: "Worked for 12s · 3 tools",
// open while running (with the live note), collapsed when done; inside, one
// line per tool with a status dot, and notes / notices between them.

import { formatDuration } from '../util.js';
import { activitySummary } from '../state.js';
import { toSafeHtml } from './markdown.js';

function headline(message, summary, live) {
  const running = message.status === 'running';
  const toolsText = summary.tools === 1 ? '1 tool' : `${summary.tools} tools`;
  if (running) {
    const note = message.note || (summary.running ? 'Running tools…' : 'Working…');
    return summary.tools ? `${note} · ${toolsText}` : note;
  }
  const ms = message.duration_ms != null
    ? message.duration_ms
    : (live && message.started_at ? Date.now() - message.started_at : null);
  const worked = ms != null ? `Worked for ${formatDuration(ms)}` : 'Worked';
  if (message.status === 'canceled') return summary.tools ? `Stopped · ${toolsText}` : 'Stopped';
  return summary.tools ? `${worked} · ${toolsText}` : worked;
}

function preview(text) {
  const s = String(text ?? '').trim();
  return s.length > 240 ? `${s.slice(0, 239)}…` : s;
}

/** Build (or rebuild) the strip. `open` state is kept on the <details>. */
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

  const sum = document.createElement('summary');
  sum.className = 'activity-summary';
  sum.addEventListener('click', () => { details.dataset.userToggled = '1'; });
  const dot = document.createElement('span');
  dot.className = `dot ${running ? 'running' : summary.errors || message.status === 'failed' ? 'error' : message.status === 'canceled' ? 'neutral' : 'done'}`;
  dot.setAttribute('aria-hidden', 'true');
  const text = document.createElement('span');
  text.className = 'activity-headline';
  text.textContent = headline(message, summary, live);
  const chev = document.createElement('span');
  chev.className = 'chev';
  chev.setAttribute('aria-hidden', 'true');
  sum.append(dot, text, chev);
  details.appendChild(sum);

  const list = document.createElement('ul');
  list.className = 'activity-list';
  for (const part of message.parts || []) {
    if (part.type === 'tool') {
      const li = document.createElement('li');
      li.className = `tool status-${part.status || 'running'}`;
      const d = document.createElement('span');
      d.className = `dot ${part.status === 'running' ? 'running' : part.status === 'error' ? 'error' : 'done'}`;
      d.setAttribute('aria-hidden', 'true');
      const label = document.createElement('span');
      label.className = 'tool-label';
      const agent = document.createElement('span');
      agent.className = 'tool-agent';
      agent.textContent = part.agent || 'orchestrator';
      const name = document.createElement('span');
      name.className = 'tool-name';
      name.textContent = part.name || 'tool';
      label.append(agent, document.createTextNode(' '), name);
      const status = document.createElement('span');
      status.className = 'tool-status';
      status.textContent = part.status === 'running' ? 'running' : part.status === 'error' ? 'failed' : 'done';
      li.append(d, label, status);
      const args = preview(part.args);
      const out = preview(part.output);
      if (args || out) {
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
        li.appendChild(pre);
      }
      list.appendChild(li);
    } else if (part.type === 'note') {
      const li = document.createElement('li');
      li.className = 'note';
      li.textContent = part.text;
      list.appendChild(li);
    } else if (part.type === 'message') {
      const li = document.createElement('li');
      li.className = 'notice md';
      li.innerHTML = toSafeHtml(part.text);
      list.appendChild(li);
    }
  }
  details.appendChild(list);
  return details;
}
