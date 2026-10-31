// Unit tests of the web chat client's pure modules (static/chat/).
// Run:  node tests/test_webchat_ui.mjs
import { JSDOM } from 'jsdom';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);

let failed = 0;
const check = (label, cond, detail = '') => {
  if (cond) console.log(`  PASS  ${label}`);
  else { failed++; console.log(`  FAIL  ${label}   ${detail}`); }
};
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// A DOM before the modules load (render/markdown.js is imported indirectly).
const dom = new JSDOM('<!doctype html><html><body><div id="app"></div></body></html>', { url: 'http://127.0.0.1:7935/chat' });
globalThis.window = dom.window;
globalThis.document = dom.window.document;
Object.defineProperty(globalThis, "navigator", { value: dom.window.navigator, configurable: true });
globalThis.localStorage = dom.window.localStorage;
globalThis.matchMedia = () => ({ matches: false, addEventListener() {} });
globalThis.marked = require('../static/vendor/marked.min.js').marked;
globalThis.DOMPurify = require('../static/vendor/purify.min.js')(dom.window);
globalThis.hljs = require('../static/vendor/highlight.min.js');

const { createSSEParser } = await import('../static/chat/api.js');
const { applyEvent, applyStreamEvent, newAssistantMessage, activitySummary, activityTree, usagePart, addUsage } = await import('../static/chat/state.js');
const util = await import('../static/chat/util.js');
const { toSafeHtml, renderMarkdown } = await import('../static/chat/render/markdown.js');

// ---------------------------------------------------------------- SSE parser
console.log('\n== SSE parser ==');
{
  const got = [];
  const p = createSSEParser((e) => got.push(e));
  const stream = 'event: start\ndata: {"user_message_id":"u1"}\n\n: ping\n\nevent: text\ndata: {"delta":"Hel';
  // split in awkward places: inside a field name, inside the JSON, across CRLF
  const chunks = [stream.slice(0, 3), stream.slice(3, 20), stream.slice(20, 61), stream.slice(61),
    'lo"}\n\nev', 'ent: note\r\ndata: {"text":"a"}\r\n\r\n', 'data: one\ndata: two\n\n', 'event: done\ndata: {"status":"done"}'];
  for (const c of chunks) p.push(c);
  p.end();
  check('five events dispatched', got.length === 5, String(got.length));
  check('start parsed', got[0].type === 'start' && got[0].data.user_message_id === 'u1');
  check('comment line dropped', !got.some((e) => e.raw.includes('ping')));
  check('text delta reassembled across chunks', got[1].type === 'text' && got[1].data.delta === 'Hello');
  check('CRLF lines handled', got[2].type === 'note' && got[2].data.text === 'a');
  check('multi-line data joined, default type message', got[3].type === 'message' && got[3].data === 'one\ntwo');
  check('trailing event without blank line flushed by end()', got[4].type === 'done' && got[4].data.status === 'done');
}
{
  const got = [];
  const p = createSSEParser((e) => got.push(e));
  p.push('event: x\n\n'); // no data -> nothing
  p.push('data:{"a":1}\n\n'); // no space after colon
  check('event without data is not dispatched', got.length === 1 && got[0].data.a === 1);
}

// ---------------------------------------------------------------- reducer
console.log('\n== parts reducer ==');
{
  let m = newAssistantMessage();
  const original = m;
  m = applyEvent(m, { type: 'text', data: { delta: 'Hel' } });
  m = applyEvent(m, { type: 'text', data: { delta: 'lo' } });
  check('text deltas merge into one part', m.parts.length === 1 && m.parts[0].text === 'Hello' && m.content === 'Hello');
  check('reducer does not mutate', original.parts.length === 0 && original.content === '');
  m = applyEvent(m, { type: 'tool', data: { id: 't1', agent: 'btp-admin', name: 'list', status: 'running', args: 'x=1' } });
  m = applyEvent(m, { type: 'note', data: { text: 'Working' } });
  m = applyEvent(m, { type: 'note', data: { text: 'Still working' } });
  m = applyEvent(m, { type: 'tool', data: { id: 't1', status: 'done', output: '3 rows' } });
  const tool = m.parts.find((p) => p.type === 'tool');
  check('tool update merges by id and keeps agent/name/args', tool.status === 'done' && tool.agent === 'btp-admin' && tool.name === 'list' && tool.args === 'x=1' && tool.output === '3 rows');
  check('consecutive notes collapse to one part, latest kept live', m.parts.filter((p) => p.type === 'note').length === 1 && m.note === 'Still working');
  m = applyEvent(m, { type: 'text', data: { delta: ' world' } });
  check('text after a tool opens a new text part', m.parts.filter((p) => p.type === 'text').length === 2 && m.content === 'Hello world');
  m = applyEvent(m, { type: 'file', data: { file_id: 'f1', name: 'r.xlsx', media_type: 'x', size: 10 } });
  m = applyEvent(m, { type: 'file', data: { file_id: 'f1', name: 'r.xlsx', media_type: 'x', size: 10 } });
  check('file parts are deduplicated by id', m.parts.filter((p) => p.type === 'file').length === 1);
  m = applyEvent(m, { type: 'message', data: { text: 'Sign in [here](/oauth/login?x=1)' } });
  check('message notice stored as a part', m.parts.some((p) => p.type === 'message'));
  check('activity summary counts', eq([activitySummary(m).tools, activitySummary(m).running, activitySummary(m).hasActivity], [1, 0, true]));
  check('tool without agent_id is the orchestrator\'s own (null)', tool.agent_id === null);
  const unknown = applyEvent(m, { type: 'whatever', data: {} });
  check('unknown event is ignored', unknown === m);
  const done = applyEvent(m, { type: 'done', data: { message_id: 'srv-1', status: 'done' } });
  check('done sets status, server id, duration and clears the note', done.status === 'done' && done.server_id === 'srv-1' && done.note === null && typeof done.duration_ms === 'number');
  const err = applyEvent(m, { type: 'error', data: { code: 'canceled', message: 'stopped' } });
  check('error canceled -> canceled status', err.status === 'canceled' && err.error.code === 'canceled');
  const err2 = applyEvent(m, { type: 'error', data: { code: 'model_unavailable', message: 'x' } });
  check('other error -> failed', err2.status === 'failed');
}
{
  const conv = { id: 'c', title: 'New chat', status: 'idle', messages: [
    { id: 'u-local', role: 'user', content: 'hi', files: [{ name: 'a.png', media_type: 'image/png', size: 3 }] },
    newAssistantMessage(),
  ] };
  const aid = conv.messages[1].id;
  let c = applyStreamEvent(conv, 'u-local', aid, { type: 'start', data: { user_message_id: 'U1', assistant_message_id: 'A1', files: [{ id: 'F1', name: 'a.png', media_type: 'image/png', size: 3 }] } });
  check('start binds server ids and stored uploads', c.messages[0].server_id === 'U1' && c.messages[0].files[0].id === 'F1' && c.messages[0].files[0].kind === 'upload' && c.messages[1].server_id === 'A1' && c.status === 'running');
  c = applyStreamEvent(c, 'u-local', aid, { type: 'title', data: { title: 'Hello' } });
  check('title renames the conversation', c.title === 'Hello');
  c = applyStreamEvent(c, 'u-local', aid, { type: 'text', data: { delta: 'ok' } });
  c = applyStreamEvent(c, 'u-local', aid, { type: 'done', data: { message_id: 'A1', status: 'done' } });
  check('done ends the run at conversation level', c.status === 'idle' && c.messages[1].status === 'done' && c.messages[1].content === 'ok');
  check('original conversation untouched', conv.title === 'New chat' && conv.messages[1].content === '');
}

// ---------------------------------------------------------------- agents, usage, tree
console.log('\n== agent parts, usage part, activity tree ==');
{
  let m = newAssistantMessage();
  m.started_at = 1_000;
  m = applyEvent(m, { type: 'agent', data: { id: 'a1', agent: 'btp-admin', parent: null, status: 'running', prompt: 'List the subaccounts' } });
  m = applyEvent(m, { type: 'tool', data: { id: 't1', agent: 'btp-admin', agent_id: 'a1', name: 'list', status: 'running', args: 'x' } });
  m = applyEvent(m, { type: 'agent', data: { id: 'a2', agent: 'peer', parent: 'a1', status: 'running', prompt: 'rank them' } });
  m = applyEvent(m, { type: 'tool', data: { id: 't2', agent: 'peer', agent_id: 'a2', name: 'rank', status: 'running' } });
  m = applyEvent(m, { type: 'tool', data: { id: 't0', agent: null, agent_id: null, name: 'own', status: 'running' } });
  let s = activitySummary(m, 5_000);
  check('live summary: agents, tools, running, duration from started_at', eq([s.agents, s.tools, s.running, s.duration, s.tokens], [2, 3, 5, 4_000, 0]));
  m = applyEvent(m, { type: 'tool', data: { id: 't2', status: 'done', output: 'ok', duration_ms: 300 } });
  m = applyEvent(m, { type: 'agent', data: { id: 'a2', status: 'done', output: '2 flagged', duration_ms: 900, usage: { input_tokens: 600, output_tokens: 100, requests: 1 } } });
  m = applyEvent(m, { type: 'tool', data: { id: 't1', status: 'done', output: '3', duration_ms: 1200 } });
  m = applyEvent(m, { type: 'agent', data: { id: 'a1', status: 'error', output: 'timed out', duration_ms: 5000, usage: { input_tokens: 1000, output_tokens: 200, requests: 2 } } });
  m = applyEvent(m, { type: 'tool', data: { id: 't0', status: 'done', duration_ms: 50 } });
  const a1 = m.parts.find((p) => p.type === 'agent' && p.id === 'a1');
  const a2 = m.parts.find((p) => p.type === 'agent' && p.id === 'a2');
  const t1 = m.parts.find((p) => p.type === 'tool' && p.id === 't1');
  check('agent frames merge by id, keeping prompt and parent', a1.status === 'error' && a1.prompt === 'List the subaccounts' && a1.parent === null && a1.output === 'timed out' && a1.duration_ms === 5000 && a1.usage.input_tokens === 1000);
  check('nested agent keeps its parent through the closing frame', a2.parent === 'a1' && a2.status === 'done' && a2.prompt === 'rank them');
  check('tool keeps agent_id and gets duration_ms', t1.agent_id === 'a1' && t1.duration_ms === 1200 && t1.args === 'x' && t1.output === '3');
  check('one agent part per id', m.parts.filter((p) => p.type === 'agent').length === 2);
  // tokens from the agent parts while there is no usage part
  m.status = 'done';
  m.duration_ms = 7000;
  s = activitySummary(m);
  check('stored summary sums agent usage without a usage part, duration from the message', eq([s.tokens, s.duration, s.errors, s.running], [1900, 7000, 1, 0]));
  // the tree
  const tree = activityTree(m);
  check('top level: own tool and the top agent, in order of first appearance', eq(tree.map((n) => [n.kind, n.part.id]), [['agent', 'a1'], ['tool', 't0']]));
  const a1n = tree[0];
  check('children of a1: its tool then the nested agent', eq(a1n.children.map((n) => [n.kind, n.part.id]), [['tool', 't1'], ['agent', 'a2']]));
  check('nested agent holds its own tool', eq(a1n.children[1].children.map((n) => n.part.id), ['t2']));
  // usage part
  const usage = { input_tokens: 1240, output_tokens: 380, cache_read_tokens: 0, total_tokens: 1620, requests: 4, tool_calls: 3, model: 'gpt-4o', duration_ms: 8100, agents: [{ id: 'a1', agent: 'btp-admin', input_tokens: 820, output_tokens: 120, requests: 2, model: 'gpt-4o' }] };
  m = applyEvent(m, { type: 'usage', data: usage });
  m = applyEvent(m, { type: 'usage', data: { ...usage, total_tokens: 1621 } });
  check('usage part stored once, a repeat replaces it', m.parts.filter((p) => p.type === 'usage').length === 1 && usagePart(m).total_tokens === 1621 && usagePart(m).model === 'gpt-4o');
  s = activitySummary(m);
  check('summary prefers the usage part for tokens and duration', s.tokens === 1621 && s.duration === 8100);
  m = applyEvent(m, { type: 'done', data: { message_id: 'A', status: 'done', duration_ms: 8200 } });
  check('done.duration_ms is kept on the message', m.duration_ms === 8200);
  check('usage is not a tree node', !activityTree(m).some((n) => n.kind === 'usage'));
  // orphan ownership falls back to the top level
  const orphan = { parts: [{ type: 'tool', id: 'x', agent_id: 'nobody', name: 'n', status: 'done' }] };
  check('a tool whose owner is unknown stays visible at the top', activityTree(orphan).length === 1);
  const cyc = { parts: [{ type: 'agent', id: 'p', parent: 'q', status: 'done' }, { type: 'agent', id: 'q', parent: 'p', status: 'done' }] };
  check('a parent cycle does not loop', activityTree(cyc).length >= 1);
}
{
  const conv = { id: 'c', title: 't', status: 'running', usage: { input_tokens: 100, output_tokens: 10, total_tokens: 110, requests: 1, turns: 1 }, messages: [{ id: 'u', role: 'user', content: 'x' }, newAssistantMessage()] };
  const aid = conv.messages[1].id;
  let c = applyStreamEvent(conv, 'u', aid, { type: 'usage', data: { input_tokens: 1240, output_tokens: 380, total_tokens: 1620, requests: 4 } });
  check('usage event adds the turn to the conversation total', eq(c.usage, { input_tokens: 1340, output_tokens: 390, total_tokens: 1730, requests: 5, turns: 2 }));
  c = applyStreamEvent(c, 'u', aid, { type: 'usage', data: { input_tokens: 1250, output_tokens: 380, total_tokens: 1630, requests: 4 } });
  check('a repeated usage event counts the turn once', eq(c.usage, { input_tokens: 1350, output_tokens: 390, total_tokens: 1740, requests: 5, turns: 2 }));
  check('addUsage from nothing', eq(addUsage(null, { input_tokens: 5, output_tokens: 1, requests: 1 }), { input_tokens: 5, output_tokens: 1, total_tokens: 6, requests: 1, turns: 1 }));
}

// ---------------------------------------------------------------- number formatters
console.log('\n== token and elapsed formatters ==');
check('tokens exact below 10,000 with separators', util.formatTokens(1240) === '1,240' && util.formatTokens(380) === '380' && util.formatTokens(9999) === '9,999');
check('tokens in k from 10,000', util.formatTokens(12_400) === '12.4k' && util.formatTokens(10_000) === '10k' && util.formatTokens(999_950) === '1000k');
check('tokens in M', util.formatTokens(1_250_000) === '1.3M' && util.formatTokens(2_000_000) === '2M');
check('tokens garbage', util.formatTokens('x') === '' && util.formatTokens(-5) === '' && util.formatTokens(0) === '0');
check('elapsed one decimal under 10 s', util.formatElapsed(8_100) === '8.1 s' && util.formatElapsed(0) === '0.0 s');
check('elapsed whole seconds under a minute', util.formatElapsed(12_400) === '12 s');
check('elapsed minutes', util.formatElapsed(65_000) === '1m 05s');

// ---------------------------------------------------------------- dates
console.log('\n== date grouping ==');
{
  const now = new Date('2026-10-31T10:00:00');
  const iso = (d, h = 9) => { const x = new Date(now); x.setDate(x.getDate() - d); x.setHours(h, 0, 0, 0); return x.toISOString(); };
  check('today', util.dateGroup(iso(0, 1), now) === 'Today');
  check('late yesterday is Yesterday', util.dateGroup(iso(1, 23), now) === 'Yesterday');
  check('seven days ago is Previous 7 days', util.dateGroup(iso(7), now) === 'Previous 7 days');
  check('eight days ago is Older', util.dateGroup(iso(8), now) === 'Older');
  check('garbage is Older', util.dateGroup('nope', now) === 'Older');
  const groups = util.groupConversations([
    { id: 1, title: 'a', updated_at: iso(8) }, { id: 2, title: 'b', updated_at: iso(0, 2) },
    { id: 3, title: 'c', updated_at: iso(0, 5) }, { id: 4, title: 'd', updated_at: iso(3) },
  ], now);
  check('groups in order, empty ones left out', eq(groups.map((g) => g.label), ['Today', 'Previous 7 days', 'Older']));
  check('newest first inside a group', eq(groups[0].items.map((c) => c.id), [3, 2]));
  check('title filter is case-insensitive', util.filterConversations([{ title: 'Hello World' }, { title: 'x' }], 'WORLD').length === 1);
  check('blank filter keeps all', util.filterConversations([{ title: 'a' }], '  ').length === 1);
}

// ---------------------------------------------------------------- sizes
console.log('\n== file size formatter ==');
check('bytes', util.formatSize(0) === '0 B' && util.formatSize(999) === '999 B');
check('KB with one decimal under 10', util.formatSize(1536) === '1.5 KB');
check('whole KB has no .0', util.formatSize(2048) === '2 KB');
check('KB rounded over 10', util.formatSize(24 * 1024 + 300) === '24 KB');
check('MB', util.formatSize(10 * 1024 * 1024) === '10 MB' && util.formatSize(1.25 * 1024 * 1024) === '1.3 MB');
check('GB', util.formatSize(3 * 1024 ** 3) === '3 GB');
check('garbage is empty', util.formatSize('x') === '' && util.formatSize(-1) === '');
check('duration', util.formatDuration(12_400) === '12s' && util.formatDuration(65_000) === '1m 05s');

// ---------------------------------------------------------------- limits
console.log('\n== attachment limit checks ==');
{
  const limits = { max_file_bytes: 1000, max_files: 2, media_types: ['image/png', 'application/pdf', util.EXTENSION_TYPES.docx] };
  const f = (name, size, type = '') => ({ name, size, type });
  const r = util.checkAttachments([], [f('a.png', 10), f('b.exe', 10), f('c.pdf', 2000), f('d.docx', 5, 'application/octet-stream'), f('e.pdf', 1), f('empty.png', 0)], limits);
  check('accepted the two that fit', eq(r.accepted.map((x) => x.name), ['a.png', 'd.docx']));
  check('extension beats a vague File.type', r.accepted.some((x) => x.name === 'd.docx'));
  check('refusal reasons', eq(r.rejected.map((x) => [x.file.name, x.reason]), [['b.exe', 'type'], ['c.pdf', 'too_large'], ['e.pdf', 'too_many'], ['empty.png', 'too_many']]));
  check('fixed texts', r.rejected.every((x) => x.text === util.LIMIT_TEXTS[x.reason]));
  const r2 = util.checkAttachments([f('x.png', 1), f('y.png', 1)], [f('z.png', 1)], limits);
  check('existing chips count towards max_files', r2.accepted.length === 0 && r2.rejected[0].reason === 'too_many');
  const r3 = util.checkAttachments([], [f('z.png', 0)], limits);
  check('empty file refused', r3.rejected[0].reason === 'empty');
  const text = util.describeRejections(r.rejected, limits);
  check('toast text names the limits without file names', text.includes('At most 2 files') && text.includes('under 1000 B') && !text.includes('b.exe'));
  check('defaults without limits', util.checkAttachments([], [f('a.png', 5 * 1024 * 1024)], null).accepted.length === 1);
  check('mediaTypeOf by extension', util.mediaTypeOf(f('A.JPG', 1)) === 'image/jpeg' && util.mediaTypeOf(f('noext', 1, 'text/plain')) === 'text/plain');
  check('fileKind', eq(['r.xlsx', 's.docx', 'p.pdf', 'i.png', 'd.json', 't.txt', 'z.bin'].map((n) => util.fileKind('', n)), ['sheet', 'doc', 'pdf', 'file', 'code', 'text', 'file']));
  check('fileKind by media type', util.fileKind('image/jpeg', 'x') === 'image' && util.fileKind('text/csv', 'x') === 'sheet');
  check('titleFromText', util.titleFromText('\n\n  Hello there \nmore') === 'Hello there' && util.titleFromText('x'.repeat(200)).length === 120 && util.titleFromText('') === 'New chat');
}

// ---------------------------------------------------------------- markdown
console.log('\n== markdown rendering is sanitized ==');
{
  const html = toSafeHtml('Hi <img src=x onerror=alert(1)> [a](https://x.y) <script>alert(2)</script>\n\n```python\nprint(1)\n```\n\n<a href="javascript:alert(3)">j</a>');
  check('event handlers stripped', !html.includes('onerror'));
  check('script stripped', !html.includes('<script'));
  check('javascript: href stripped', !html.includes('javascript:'));
  check('links open in a new tab with noopener', /<a[^>]*href="https:\/\/x\.y"[^>]*target="_blank"/.test(html) && html.includes('rel="noopener noreferrer"'));
  const div = document.createElement('div');
  renderMarkdown(div, '# T\n\n```python\nprint("x")\n```\n\n| a |\n| - |\n| 1 |', { final: false });
  check('code block gets a header with copy button', !!div.querySelector('.codeblock .codeblock-copy') && div.querySelector('.codeblock-lang').textContent === 'python');
  check('highlight.js applied', !!div.querySelector('code.hljs .hljs-built_in, code.hljs .hljs-string'));
  check('table rendered', !!div.querySelector('table td'));
  renderMarkdown(div, '```mermaid\nflowchart LR\nA-->B\n```', { final: false });
  check('mermaid fence left as source while streaming', !!div.querySelector('pre.mermaid-source'));
}

console.log(failed ? `\n${failed} FAILED` : '\nall passed');
process.exit(failed ? 1 : 0);
