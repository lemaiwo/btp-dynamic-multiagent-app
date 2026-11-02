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
const my = await import('../static/chat/myagents.js');
const { attachmentFilename } = await import('../static/chat/api.js');
const { renderEmpty, DEVELOPER_PROMPTS, DEVELOPER_GREETING, createMessageList } = await import('../static/chat/render/messages.js');
const { contextViewModel, excludedFileIds } = await import('../static/chat/state.js');
const { fileCard, fileCards, NOT_IN_CONTEXT } = await import('../static/chat/render/files.js');
const { renderContextBody, IN_CONTEXT } = await import('../static/chat/render/contextDrawer.js');

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

// ---------------------------------------------------------------- personal agents and skills (addendum c)
console.log('\n== agent / skill validators ==');
{
  const ok = { name: 'Quota watcher', description: 'one line', instructions: 'Do the thing', skills: [], consults: [], model: null };
  check('a valid agent has no errors', eq(my.validateAgent(ok), {}));
  check('name pattern: must start with a letter or digit', my.validateAgent({ ...ok, name: '-x' }).name === my.TEXTS.name && my.validateAgent({ ...ok, name: '' }).name === my.TEXTS.name);
  check('name pattern: allowed characters only, parentheses included', my.validateAgent({ ...ok, name: 'a/b' }).name === my.TEXTS.name && !my.validateAgent({ ...ok, name: 'a_b.c-d 9' }).name && !my.validateAgent({ ...ok, name: 'Report (2)' }).name);
  check('name at most 64 characters', !my.validateAgent({ ...ok, name: 'a'.repeat(64) }).name && my.validateAgent({ ...ok, name: 'a'.repeat(65) }).name === my.TEXTS.name);
  check('name taken (case-insensitive) is the fixed text', my.validateAgent(ok, { taken: new Set(['quota watcher']) }).name === my.TEXTS.name_taken_agent);
  check('takenNames lower-cases and leaves the edited row out', eq([...my.takenNames([{ id: 1, name: 'A' }, { id: 2, name: 'Bee' }], 2)], ['a']));
  check('description over 300 or with a line break', my.validateAgent({ ...ok, description: 'x'.repeat(301) }).description === my.TEXTS.description && my.validateAgent({ ...ok, description: 'a\nb' }).description === my.TEXTS.description && !my.validateAgent({ ...ok, description: 'x'.repeat(300) }).description);
  check('instructions 1..20,000', my.validateAgent({ ...ok, instructions: '' }).instructions === my.TEXTS.instructions && my.validateAgent({ ...ok, instructions: 'x'.repeat(20_001) }).instructions === my.TEXTS.instructions && !my.validateAgent({ ...ok, instructions: 'x'.repeat(20_000) }).instructions);
  check('control characters refused, tab and newline kept', my.validateAgent({ ...ok, instructions: 'a\u0007b' }).instructions === my.TEXTS.control && !my.validateAgent({ ...ok, instructions: 'a\tb\nc' }).instructions);
  check('at most 20 skills and 10 consults', my.validateAgent({ ...ok, skills: Array(21).fill({ scope: 'mine', name: 's' }) }).skills === my.TEXTS.skills && my.validateAgent({ ...ok, consults: Array(11).fill('a') }).consults === my.TEXTS.consults && !my.validateAgent({ ...ok, skills: Array(20).fill({ scope: 'mine', name: 's' }), consults: Array(10).fill('a') }).skills);
  check('model must be one of the list, null or empty is the default', my.validateAgent({ ...ok, model: 'nope', models: ['gpt'] }).model === my.TEXTS.model && !my.validateAgent({ ...ok, model: 'gpt', models: ['gpt'] }).model && !my.validateAgent({ ...ok, model: '', models: ['gpt'] }).model);
  const sk = { name: 'report', description: '', content: 'x' };
  check('a valid skill has no errors', eq(my.validateSkill(sk), {}));
  check('skill content 1..50,000', my.validateSkill({ ...sk, content: '' }).content === my.TEXTS.content && my.validateSkill({ ...sk, content: 'x'.repeat(50_001) }).content === my.TEXTS.content && !my.validateSkill({ ...sk, content: 'x'.repeat(50_000) }).content);
  check('skill name taken text', my.validateSkill(sk, { taken: new Set(['report']) }).name === my.TEXTS.name_taken_skill);
  check('fieldErrors maps a 422 loc to the field with our text, never the message', eq(my.fieldErrors([{ loc: ['body', 'skills', 0, 'name'], msg: 'SECRET' }, { loc: ['body', 'nope'], msg: 'x' }, { loc: ['body', 'name'], msg: 'y' }], ['name', 'skills']), { skills: my.TEXTS.skills, name: my.TEXTS.name }));
}

console.log('\n== export query, import result, picker ==');
{
  check('nothing selected exports everything', my.exportQuery([], []) === '');
  check('agents and skills joined by commas, deduplicated, encoded', my.exportQuery(['a1', 'a2', 'a1'], ['s 1']) === '?agents=a1,a2&skills=s%201');
  check('skills only', my.exportQuery([], ['s1']) === '?skills=s1');
  const r = my.formatImportResult({ agents: { created: 1, replaced: 0, skipped: 2, renamed: [{ from: 'Quota watcher', to: 'Quota watcher 2' }] }, skills: { created: 3, replaced: 1, skipped: 0, renamed: [] },
    dropped: [{ item: 'agent:Quota watcher', field: 'consults', name: 'btp-prod' }, { item: 'agent:Quota watcher', field: 'skills', name: 'gone' }, { item: 'agent:Quota watcher', field: 'model', name: null }],
    errors: [{ item: 'skill:bad', field: 'content', rule: 'too_long' }, { item: 'agent:#3', field: 'name', rule: 'is not a valid name' }, { item: 'agent:x', field: 'name', rule: '' }] });
  check('summary line per kind', eq(r.summary, ['1 agent created, 0 replaced, 2 skipped', '3 skills created, 1 replaced, 0 skipped']));
  check('renamed from -> to', eq(r.renamed, ['agent Quota watcher → Quota watcher 2']));
  check('dropped references name the item, the kind and the name (a dropped model has none)', r.dropped.length === 3 && r.dropped[0].includes('consulted agent btp-prod') && r.dropped[1].includes('skill gone') && r.dropped[2] === 'agent:Quota watcher: model does not exist here and was dropped');
  check('errors: a short code is worded, the server\'s fixed text is shown as is, empty falls back', r.errors[0] === 'skill:bad: content \u2014 is too long; not imported' && r.errors[1] === 'agent:#3: name \u2014 is not a valid name; not imported' && r.errors[2] === 'agent:x: name \u2014 was not accepted; not imported' && r.ok === false);
  check('an empty answer is ok with zero lines', my.formatImportResult({}).ok && my.formatImportResult(null).summary.length === 2);
  const catalog = { orchestrator: { ref: null, name: 'Orchestrator' }, agents: [{ ref: 'admin:btp-admin', name: 'btp-admin' }], skills: [] };
  const mine = [{ id: '1', ref: 'mine:1', name: 'Quota watcher' }];
  const g = my.pickerGroups(catalog, mine, null);
  check('three groups in order with the orchestrator first', eq(g.map((x) => x.label), ['Orchestrator', 'My agents', 'Admin agents · read-only']) && g[0].options[0].value === '');
  check('option values are the refs', g[1].options[0].value === 'mine:1' && g[2].options[0].value === 'admin:btp-admin');
  check('an empty group is left out', my.pickerGroups(catalog, [], null).length === 2);
  const gone = my.pickerGroups(catalog, mine, 'mine:dead', 'Old one (deleted)');
  check('a ref in no group gets a disabled option so the select shows it', gone[gone.length - 1].options[0].disabled === true && gone[gone.length - 1].options[0].label === 'Old one (deleted)');
  check('agentNameOf resolves mine, admin, orchestrator and falls back to the stored name', my.agentNameOf('mine:1', catalog, mine) === 'Quota watcher' && my.agentNameOf('admin:btp-admin', catalog, mine) === 'btp-admin' && my.agentNameOf(null, catalog, mine) === 'Orchestrator' && my.agentNameOf('mine:x', catalog, mine, 'Gone (deleted)') === 'Gone (deleted)');
  check('attachment filename: RFC 5987 first, then quoted, then bare, then the fallback', attachmentFilename("attachment; filename*=UTF-8''webchat-bundle-2026-10-31.json") === 'webchat-bundle-2026-10-31.json' && attachmentFilename('attachment; filename="a b.json"') === 'a b.json' && attachmentFilename('attachment; filename=x.json') === 'x.json' && attachmentFilename('') === 'webchat-bundle.json' && attachmentFilename('attachment; filename="../x.json"') === '..x.json');
}

// ---------------------------------------------------------------- basic and advanced access (addendum d)
console.log('\n== tiers, badges and the access-aware picker ==');
{
  const adv = { ref: 'admin:abap-developer-deep', name: 'abap-developer-deep', tier: 'advanced' };
  const basic = { ref: 'admin:btp-admin', name: 'btp-admin', tier: 'basic' };
  const catalog = { orchestrator: { ref: null, name: 'Orchestrator' }, agents: [basic, adv], skills: [] };
  const mine = [{ id: '1', ref: 'mine:1', name: 'Quota watcher', tier: 'basic', deep: false }, { id: '2', ref: 'mine:2', name: 'ABAP reviewer', tier: 'advanced', deep: true }];
  check('tierOf: the server tier, deep implies advanced, anything else is basic', my.tierOf(adv) === 'advanced' && my.tierOf(basic) === 'basic' && my.tierOf({ name: 'x', deep: true }) === 'advanced' && my.tierOf({ name: 'x', tier: 'nope' }) === 'basic' && my.tierOf(null) === 'basic' && my.tierOf({ deep: 'true' }) === 'basic');
  check('optionLabel carries the Developer badge for an advanced agent only', my.optionLabel(adv) === 'abap-developer-deep · Developer' && my.optionLabel(basic) === 'btp-admin' && my.DEVELOPER_BADGE === 'Developer');
  check('advancedAgentNames lists the advanced catalogue agents', eq([...my.advancedAgentNames(catalog)], ['abap-developer-deep']) && my.advancedAgentNames(null).size === 0);
  const gBasic = my.pickerGroups(catalog, mine, null, '', { access: 'basic' });
  check('basic access hides advanced admin and personal agents', eq(gBasic.map((g) => g.options.map((o) => o.value)), [[''], ['mine:1'], ['admin:btp-admin']]));
  check('the default access is basic', eq(my.pickerGroups(catalog, mine, null), gBasic));
  const gAdv = my.pickerGroups(catalog, mine, null, '', { access: 'advanced' });
  check('advanced access lists them with the badge in the label', eq(gAdv[1].options.map((o) => o.label), ['Quota watcher', 'ABAP reviewer · Developer']) && eq(gAdv[2].options.map((o) => o.label), ['btp-admin', 'abap-developer-deep · Developer']));
  const stale = my.pickerGroups(catalog, mine, 'admin:abap-developer-deep', 'abap-developer-deep', { access: 'basic' });
  check('an advanced current ref in a basic conversation is shown disabled under Unavailable', stale[stale.length - 1].label === 'Unavailable' && stale[stale.length - 1].options[0].disabled === true);
  check('refOffered follows the access; the orchestrator always is', my.refOffered('admin:abap-developer-deep', catalog, mine, 'basic') === false && my.refOffered('admin:abap-developer-deep', catalog, mine, 'advanced') === true && my.refOffered('mine:2', catalog, mine, 'basic') === false && my.refOffered('mine:1', catalog, mine, 'basic') === true && my.refOffered(null, catalog, mine, 'basic') === true && my.refOffered('mine:dead', catalog, mine, 'advanced') === false);
  check('agentOf finds the row behind a ref', my.agentOf('mine:2', catalog, mine).name === 'ABAP reviewer' && my.agentOf('admin:abap-developer-deep', catalog, mine).tier === 'advanced' && my.agentOf('admin:none', catalog, mine) === null && my.agentOf(null, catalog, mine) === null);
}

console.log('\n== developer-only validators and import lines ==');
{
  const ok = { name: 'Quota watcher', description: '', instructions: 'Do', skills: [], consults: [], model: null };
  const advancedNames = new Set(['abap-developer-deep']);
  check('a developer may switch deep on and consult an advanced agent', eq(my.validateAgent({ ...ok, deep: true, consults: ['abap-developer-deep'] }, { developer: true, advancedNames }), {}));
  check('a non-developer: deep refused with the fixed text', my.validateAgent({ ...ok, deep: true }, { developer: false }).deep === my.TEXTS.deep && !my.validateAgent({ ...ok, deep: false }, { developer: false }).deep && !my.validateAgent(ok, { developer: false }).deep);
  check('a non-developer: an advanced consult refused with the fixed text, a basic one fine', my.validateAgent({ ...ok, consults: ['abap-developer-deep'] }, { developer: false, advancedNames }).consults === my.TEXTS.consults_advanced && !my.validateAgent({ ...ok, consults: ['btp-admin'] }, { developer: false, advancedNames }).consults);
  check('deep must be a boolean whoever sends it', my.validateAgent({ ...ok, deep: 'yes' }, { developer: true }).deep === my.TEXTS.deep);
  check('the default is a developer (older callers unchanged)', eq(my.validateAgent({ ...ok, deep: true }), {}));
  check('fieldErrors maps a 422 at deep to our text', eq(my.fieldErrors([{ loc: ['body', 'deep'], msg: 'SECRET' }], ['deep', 'name']), { deep: my.TEXTS.deep }));
  const r = my.formatImportResult({ dropped: [{ item: 'agent:ABAP reviewer', field: 'deep', name: null }, { item: 'agent:ABAP reviewer', field: 'consults', name: 'abap-developer-deep' }] });
  check('a dropped deep is worded as the developer-role refusal, a dropped consult as before', r.dropped[0] === 'agent:ABAP reviewer: Deep agent tools (needs the developer role) were dropped' && r.dropped[1] === 'agent:ABAP reviewer: consulted agent abap-developer-deep does not exist here and was dropped' && r.ok);
}

console.log('\n== the access switch state (remembered per principal) ==');
{
  const store = new Map();
  const storage = { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, v) };
  check('normalizeAccess keeps the two tiers and falls back', util.normalizeAccess('advanced') === 'advanced' && util.normalizeAccess('basic') === 'basic' && util.normalizeAccess('root') === 'basic' && util.normalizeAccess(undefined, 'advanced') === 'advanced' && util.normalizeAccess('x', 'y') === 'basic');
  check('nothing remembered: the fallback', util.rememberedAccess('a@x', 'basic', storage) === 'basic' && util.rememberedAccess('a@x', 'advanced', storage) === 'advanced');
  check('rememberAccess writes a tier under the principal key and returns it', util.rememberAccess('a@x', 'advanced', storage) === 'advanced' && store.get(util.accessStorageKey('a@x')) === 'advanced');
  check('the choice comes back for that principal only', util.rememberedAccess('a@x', 'basic', storage) === 'advanced' && util.rememberedAccess('b@x', 'basic', storage) === 'basic');
  check('a value that is no tier is never written and a stored one is ignored', util.rememberAccess('a@x', 'root', storage) === null && store.get(util.accessStorageKey('a@x')) === 'advanced' && (store.set(util.accessStorageKey('c@x'), 'root'), util.rememberedAccess('c@x', 'basic', storage) === 'basic'));
  const broken = { getItem: () => { throw new Error('private mode'); }, setItem: () => { throw new Error('private mode'); } };
  check('a storage that throws is the fallback and a silent write', util.rememberedAccess('a@x', 'advanced', broken) === 'advanced' && util.rememberAccess('a@x', 'basic', broken) === 'basic');
  check('the real localStorage round-trips', util.rememberAccess('p', 'advanced') === 'advanced' && util.rememberedAccess('p') === 'advanced');
}

console.log('\n== the developer empty state ==');
{
  const box = document.createElement('div');
  renderEmpty(box, { appName: 'App', onPrompt: () => {}, advanced: true });
  const chips = [...box.querySelectorAll('.prompt-chip')].map((b) => b.textContent);
  check('advanced: the developer heading, the Developer tag and the three developer chips (ABAP, UI5/Fiori, CAP)', box.querySelector('.greeting').textContent.startsWith(DEVELOPER_GREETING) && !!box.querySelector('.greeting .dev-tag') && eq(chips, DEVELOPER_PROMPTS) && chips.length === 3 && chips[0].startsWith('ABAP') && chips[1].startsWith('UI5/Fiori') && chips[2].startsWith('CAP'));
  renderEmpty(box, { appName: 'App', onPrompt: () => {}, advanced: false });
  check('basic: the usual heading and chips, no tag', box.querySelector('.greeting').textContent === 'What are we doing on BTP today?' && !box.querySelector('.dev-tag') && box.querySelectorAll('.prompt-chip').length === 3 && !eq([...box.querySelectorAll('.prompt-chip')].map((b) => b.textContent), DEVELOPER_PROMPTS));
  renderEmpty(box, { appName: 'App', onPrompt: () => {}, advanced: true, agentName: 'abap-developer-deep', agentDescription: 'd' });
  check('advanced with an agent picked: "Ask <agent>" and the standard chips', box.querySelector('.greeting').textContent.startsWith('Ask abap-developer-deep') && !eq([...box.querySelectorAll('.prompt-chip')].map((b) => b.textContent), DEVELOPER_PROMPTS));
}

// ---------------------------------------------------------------- the context of a conversation (addendum e)
console.log('\n== fillLevel ==');
{
  check('measured wins over the estimate', eq(util.fillLevel(20000, 12400, 128000), { tokens: 12400, pct: 9.6875, level: 'ok', measured: true }));
  check('estimated when nothing was measured', eq(util.fillLevel(12400, null, 128000), { tokens: 12400, pct: 9.6875, level: 'ok', measured: false }));
  check('no window: pct null and level ok', util.fillLevel(12400, null, null).pct === null && util.fillLevel(12400, null, 0).pct === null && util.fillLevel(500000, null, undefined).level === 'ok');
  check('warn above 70, danger above 90, the boundaries stay below', util.fillLevel(0, 70000, 100000).level === 'ok' && util.fillLevel(0, 70001, 100000).level === 'warn' && util.fillLevel(0, 90000, 100000).level === 'warn' && util.fillLevel(0, 90001, 100000).level === 'danger' && util.fillLevel(0, 150000, 100000).level === 'danger');
  check('garbage is zero tokens, never NaN', eq(util.fillLevel('x', undefined, 'y'), { tokens: 0, pct: null, level: 'ok', measured: false }) && util.fillLevel(-5, -1, 1000).tokens === 0);
  check('formatPct rounds, says <1% for a sliver and nothing for null', util.formatPct(12.4) === '12%' && util.formatPct(0.4) === '<1%' && util.formatPct(0) === '0%' && util.formatPct(null) === '' && util.formatPct(NaN) === '');
}

console.log('\n== the composer indicator text ==');
{
  check('with a window: "~12.4k / 128k"', util.contextIndicatorText({ model: { context_window: 128000 }, history: { estimated_tokens: 12400, measured_tokens: null } }) === '~12.4k / 128k');
  check('the measured figure is the one shown', util.contextIndicatorText({ model: { context_window: 128000 }, history: { estimated_tokens: 99000, measured_tokens: 12400 } }) === '~12.4k / 128k');
  check('without a window: "~12.4k tokens"', util.contextIndicatorText({ model: { id: 'x', context_window: null }, history: { estimated_tokens: 12400 } }) === '~12.4k tokens');
  check('without data: empty (the indicator hides)', util.contextIndicatorText(null) === '' && util.contextIndicatorText({}) === '' && util.contextIndicatorText({ history: 'no' }) === '');
}

const SAMPLE_CONTEXT = {
  agent: { ref: 'mine:7', name: 'Quota watcher', kind: 'mine', tier: 'basic' },
  access: 'basic',
  model: { id: 'gpt-4o', context_window: 128000 },
  instructions: { visible: true, text: 'You watch the entitlements.', chars: 27, skills: [
    { scope: 'mine', name: 'weekly-quota-report', chars: 240, visible: true, text: '# Weekly\nOne table per subaccount.' },
    { scope: 'admin', name: 'btp-naming-conventions', chars: 180, visible: false, text: null },
  ] },
  history: { turns: 2, messages: 4, chars: 49600, estimated_tokens: 12400, measured_tokens: 15300, attachments: [
    { file_id: 'f1', name: 'subaccounts.png', media_type: 'image/png', size: 2048, excluded: false },
    { file_id: 'f2', name: 'quota.pdf', media_type: 'application/pdf', size: 1048576, excluded: true },
  ] },
  tools: [
    { kind: 'document', name: 'create_word_document' }, { kind: 'document', name: 'create_excel_workbook' },
    { kind: 'delegate', name: 'delegate_to_btp_admin', agent: 'btp-admin' },
    { kind: 'deep', name: 'write_todos' },
  ],
  usage: { input_tokens: 4390, output_tokens: 876, total_tokens: 5266, requests: 7, turns: 2 },
};

console.log('\n== the drawer view model ==');
{
  const vm = contextViewModel(SAMPLE_CONTEXT);
  check('who answers: agent, kind label, tier, access, model', vm.who.agent === 'Quota watcher' && vm.who.kind === 'Personal agent' && vm.who.tier === 'Basic' && vm.who.access === 'Basic' && vm.who.model === 'gpt-4o');
  check('the fill: measured wins, "12%" and "~15.3k of 128k tokens"', vm.who.window === 128000 && vm.who.pctText === '12%' && vm.who.fillText === '~15.3k of 128k tokens' && vm.who.fill.level === 'ok' && vm.who.fillSource.startsWith('measured'));
  check('instructions visible with their text', vm.instructions.visible && vm.instructions.text === 'You watch the entitlements.');
  check('skills: own with text, admin by name and length', vm.instructions.skills.length === 2 && vm.instructions.skills[0].scope === 'mine' && vm.instructions.skills[0].text.startsWith('# Weekly') && vm.instructions.skills[1].scope === 'admin' && vm.instructions.skills[1].text === null && vm.instructions.skills[1].charsText === '180 characters');
  check('history: summary, estimated and measured texts', vm.history.summary === '2 turns · 4 messages' && vm.history.estimatedText === '~12.4k tokens estimated' && vm.history.measuredText === '15.3k tokens measured on the last request');
  check('attachments with size text and the excluded flag', vm.history.attachments.length === 2 && vm.history.attachments[0].sizeText === '2 KB' && vm.history.attachments[0].excluded === false && vm.history.attachments[1].excluded === true && vm.history.attachments[1].sizeText === '1 MB');
  check('tools grouped by kind in a fixed order, the delegate target named', eq(vm.tools.map((g) => g.kind), ['document', 'deep', 'delegate']) && vm.tools[0].items.length === 2 && vm.tools[2].items[0].label === 'delegate_to_btp_admin → btp-admin' && vm.tools[2].items[0].agent === 'btp-admin');
  check('usage: total and detail', vm.usage.totalText === '5,266 tokens' && vm.usage.detail === '4,390 in / 876 out · 7 requests over 2 turns');
  const hidden = contextViewModel({ ...SAMPLE_CONTEXT, agent: { kind: 'orchestrator', name: 'Orchestrator' }, instructions: { visible: false, text: null, chars: 1234, skills: [] }, model: { id: 'm', context_window: null }, history: { ...SAMPLE_CONTEXT.history, measured_tokens: null } });
  check('hidden instructions: "1,234 characters · shown to admins only"; no window: tokens without a percentage; no measure: estimated', hidden.instructions.hiddenText === '1,234 characters · shown to admins only' && hidden.who.kind === 'Orchestrator' && hidden.who.window === null && hidden.who.pctText === '' && hidden.who.fillText === '~12.4k tokens · window unknown' && hidden.who.fillSource === 'estimated' && hidden.history.measuredText === 'not measured yet');
  check('an empty or odd answer is a usable model', contextViewModel(null).who.agent === 'Agent' && contextViewModel({ tools: 'x', history: { attachments: [null, { name: 'no id' }] } }).history.attachments.length === 0 && contextViewModel({}).tools.length === 0);
  check('excludedFileIds: the excluded ids as strings', eq([...excludedFileIds(SAMPLE_CONTEXT)], ['f2']) && excludedFileIds(null).size === 0 && excludedFileIds({ history: { attachments: [{ file_id: 3, excluded: 'true' }] } }).size === 0);
}

console.log('\n== the drawer body (text nodes only) ==');
{
  const body = document.createElement('div');
  const calls = [];
  renderContextBody(body, { ...SAMPLE_CONTEXT, agent: { ...SAMPLE_CONTEXT.agent, name: '<b>evil</b>' } }, { onToggle: async (id, ex) => { calls.push([id, ex]); return ex; } });
  const heads = [...body.querySelectorAll('.ctx-section > h3')].map((h) => h.textContent);
  check('five sections in order', eq(heads, ['Who answers', 'Instructions & skills', 'History', 'Tools this turn', 'Usage so far']));
  check('server text is a text node, never markup', body.querySelector('b') === null && body.textContent.includes('<b>evil</b>'));
  check('the fill bar is a meter at 12', body.querySelector('.fill-bar[role="meter"]').getAttribute('aria-valuenow') === '12' && body.querySelector('.fill-text').textContent.startsWith('12% · ~15.3k of 128k tokens'));
  const rows = [...body.querySelectorAll('.ctx-table tbody tr')];
  check('one attachment row per upload with an In context switch; the excluded one is marked', rows.length === 2 && rows[1].classList.contains('excluded') && rows[0].querySelector('input[role="switch"]').checked === true && rows[1].querySelector('input[role="switch"]').checked === false && rows[0].querySelector('input').getAttribute('aria-label') === `${IN_CONTEXT}: subaccounts.png`);
  const sw = rows[0].querySelector('input');
  sw.checked = false;
  sw.dispatchEvent(new dom.window.Event('change'));
  await new Promise((r) => setTimeout(r, 0));
  check('switching off posts excluded=true and marks the row', eq(calls, [['f1', true]]) && rows[0].classList.contains('excluded') && sw.disabled === false);
  const body409 = document.createElement('div');
  renderContextBody(body409, SAMPLE_CONTEXT, { onToggle: async () => { throw Object.assign(new Error('409'), { status: 409 }); } });
  const sw409 = body409.querySelector('.ctx-table tbody tr input');
  sw409.checked = false;
  sw409.dispatchEvent(new dom.window.Event('change'));
  await new Promise((r) => setTimeout(r, 0));
  check('a refused toggle (409 while a turn runs) puts the switch back and leaves the row as it was', sw409.checked === true && !sw409.closest('tr').classList.contains('excluded') && sw409.disabled === false);
  const sw409b = body409.querySelectorAll('.ctx-table tbody tr input')[1];
  sw409b.checked = true;
  sw409b.dispatchEvent(new dom.window.Event('change'));
  await new Promise((r) => setTimeout(r, 0));
  check('the same the other way round: an excluded file stays excluded', sw409b.checked === false && sw409b.closest('tr').classList.contains('excluded'));
  const body2 = document.createElement('div');
  renderContextBody(body2, { ...SAMPLE_CONTEXT, instructions: { visible: false, text: null, chars: 1234, skills: [] } });
  check('hidden instructions show the length line only', body2.querySelector('.ctx-pre') === null && body2.textContent.includes('1,234 characters · shown to admins only'));
  check('a delegate tool names its target', body2.querySelector('.ctx-target').textContent === 'btp-admin');
}

console.log('\n== the excluded state on file cards ==');
{
  const c = fileCard({ id: 'f2', name: 'quota.pdf', media_type: 'application/pdf', size: 1048576 }, { excludedIds: new Set(['f2']) });
  check('an excluded upload: class, "not in context" and the aria label', c.classList.contains('excluded') && c.querySelector('.file-sub').textContent === `1 MB · ${NOT_IN_CONTEXT}` && c.getAttribute('aria-label') === `quota.pdf, ${NOT_IN_CONTEXT}`);
  const plain = fileCard({ id: 'f1', name: 'a.png', media_type: 'image/png', size: 10 }, { excludedIds: new Set(['f2']) });
  check('another file is untouched', !plain.classList.contains('excluded') && plain.querySelector('.file-sub').textContent === '10 B');
  check('the flag on the file itself works too; the download link stays', fileCard({ id: 'f1', name: 'a.png', media_type: 'image/png', size: 10, excluded: true }).classList.contains('excluded') && !!fileCard({ id: 'f2', name: 'q.pdf', size: 1, excluded: true }).querySelector('.file-download'));
  check('fileCards passes the set through', fileCards([{ id: 'f2', name: 'q.pdf', size: 1 }], { excludedIds: new Set(['f2']) }).querySelector('.file-card.excluded') !== null);
  // through the message list: the user card follows the set across renders
  const log = document.createElement('section'); log.className = 'log';
  const col = document.createElement('div'); log.appendChild(col); document.body.appendChild(log);
  const list = createMessageList(col);
  const conv = { messages: [{ id: 'u1', role: 'user', content: 'hi', files: [{ id: 'f2', name: 'quota.pdf', media_type: 'application/pdf', size: 5, kind: 'upload' }] }, { id: 'a1', role: 'assistant', content: 'ok', parts: [], status: 'done' }] };
  list.render(conv, {});
  check('without a set the card is plain', col.querySelector('.msg.user .file-card') !== null && col.querySelector('.msg.user .file-card.excluded') === null);
  list.render(conv, { excludedIds: new Set(['f2']) });
  check('with the id in the set the user card is struck through', col.querySelector('.msg.user .file-card.excluded') !== null && col.querySelectorAll('.msg.user .file-row').length === 1);
  list.render(conv, { excludedIds: new Set() });
  check('and back', col.querySelector('.msg.user .file-card.excluded') === null && col.querySelectorAll('.msg.user .file-row').length === 1);
  log.remove();
}


console.log('\n== the viewer (addendum f) ==');
{
  const V = await import('../static/chat/render/viewer.js');
  const abap = '* file: zcl_order_check.clas.abap\nMETHOD validate.\n  LOOP AT it_items INTO DATA(ls_item).\n    READ TABLE mt_rules INTO ls_rule WITH KEY rule_id = ls_item-rule_id.\n    IF sy-subrc <> 0.\n      RAISE EXCEPTION NEW zcx_order_check( ).\n    ENDIF.\n  ENDLOOP.\nENDMETHOD.';
  const answer = `Here:\n\n\`\`\`abap\n${abap}\n\`\`\`\n\nA diagram:\n\n\`\`\`mermaid\ngraph TD; A-->B\n\`\`\`\n\nAnd a short one:\n\n\`\`\`js\nconst a = 1;\n\`\`\`\n`;
  const conv = (access) => ({ id: 'c1', access, messages: [
    { id: 'u1', role: 'user', content: 'hi', files: [{ id: 'up1', name: 'quota.pdf', media_type: 'application/pdf', size: 5, kind: 'upload' }] },
    { id: 'a1', role: 'assistant', content: answer, status: 'done', parts: [{ type: 'text', text: answer }, { type: 'file', file_id: 'g1', name: 'report.xlsx', media_type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', size: 9 }] },
  ] });

  // items derived
  const basic = V.viewerItems(conv('basic'));
  check('basic: files only (upload and generated), no code', eq(basic.files.map((f) => f.key), ['file:up1', 'file:g1']) && basic.code.length === 0);
  const adv = V.viewerItems(conv('advanced'));
  check('advanced: the code blocks too, mermaid excluded', adv.files.length === 2 && eq(adv.code.map((c) => c.lang), ['abap', 'js']));
  check('a code item: lines, name from the file: line, title', adv.code[0].lines === 9 && adv.code[0].name === 'zcl_order_check.clas.abap' && adv.code[0].title === 'zcl_order_check.clas.abap');
  check('codeBlocks skips an unclosed fence (streaming)', V.codeBlocks('```abap\nWRITE 1.\n').length === 0 && V.codeBlocks('```\nx\n```').length === 1);
  const many = { id: 'c2', access: 'advanced', messages: [{ id: 'a', role: 'assistant', content: Array.from({ length: 60 }, (_, i) => `\`\`\`sql\nSELECT ${i};\n\`\`\``).join('\n\n'), parts: [] }] };
  const capped = V.viewerItems(many).code;
  check('the latest 50 code items', capped.length === V.MAX_ITEMS && capped[0].code === 'SELECT 10;' && capped[49].code === 'SELECT 59;');

  // download names
  check('download name: "* file:" line', V.codeDownloadName(abap, 'abap', 1) === 'zcl_order_check.clas.abap');
  check('download name: "// file:", "# file:", "-- file:" lines, path stripped', V.codeDownloadName('// file: webapp/controller/Main.controller.ts\nx', 'ts', 1) === 'Main.controller.ts' && V.codeDownloadName('# file: probe.py\n', 'python', 1) === 'probe.py' && V.codeDownloadName('-- file: q.sql\nSELECT 1', 'sql', 1) === 'q.sql');
  check('download name fallback snippet-<n>.<ext> by language', V.codeDownloadName('x', 'abap', 3) === 'snippet-3.abap' && V.codeDownloadName('x', 'javascript', 2) === 'snippet-2.js' && V.codeDownloadName('x', 'yml', 4) === 'snippet-4.yaml' && V.codeDownloadName('x', 'cobol', 5) === 'snippet-5.txt' && V.codeDownloadName('x', '', 6) === 'snippet-6.txt');
  check('the second code item of the advanced conversation falls back', adv.code[1].name === 'snippet-2.js');

  // auto-open decision
  const c = conv('advanced');
  check('auto-open: a generated file of the turn wins', V.autoOpenItem(c, c.messages[1])?.key === 'file:g1');
  const noFile = { ...c, messages: [c.messages[0], { ...c.messages[1], parts: [{ type: 'text', text: answer }] }] };
  check('auto-open: advanced without a file -> the last code block of >= 5 lines', V.autoOpenItem(noFile, noFile.messages[1])?.name === 'zcl_order_check.clas.abap');
  const basicNoFile = { ...noFile, access: 'basic' };
  check('auto-open: basic without a file -> nothing', V.autoOpenItem(basicNoFile, basicNoFile.messages[1]) === null);
  const shortOnly = { id: 'c3', access: 'advanced', messages: [{ id: 'a', role: 'assistant', content: '```js\nconst a = 1;\n```', parts: [] }] };
  check('auto-open: a short block (< 5 lines) does not open', V.autoOpenItem(shortOnly, shortOnly.messages[0]) === null);

  // the panel: open, close, the closed flag
  const calls = [];
  const toggles = [];
  const fakeApi = {
    fileUrl: (id, inline) => `/chat/api/files/${id}${inline ? '?inline=1' : ''}`,
    fetchFileBlob: async () => null,
    filePreview: async (id) => {
      calls.push(id);
      if (id === 'g1') return { kind: 'sheets', name: 'report.xlsx', truncated: false, sheets: [
        { name: 'Entitlements', columns: ['A', 'B', 'C'], rows: [['Subaccount', 'Service', 'Spare'], ['b-fdn-dev', '<b>x</b>', 3], [null, null, null]], row_count: 3, truncated: false },
        { name: 'Summary', columns: ['A', 'B'], rows: [['Checked', '2026-10-31']], row_count: 1, truncated: false }] };
      if (id === 'd1') return { kind: 'document', name: 'r.docx', truncated: false, blocks: [{ type: 'heading', level: 1, text: 'Review' }, { type: 'paragraph', text: '<img src=x onerror=alert(1)>' }, { type: 'list', ordered: true, items: ['one', 'two'] }, { type: 'table', rows: [['h1', 'h2'], ['a', 'b']] }] };
      throw Object.assign(new Error('415'), { status: 415, detail: 'No preview for this file type' });
    },
  };
  const v = V.createViewer({ api: fakeApi, onToggle: (o) => toggles.push(o) });
  document.body.appendChild(v.el);
  check('the panel is a labelled region, hidden at first', v.el.getAttribute('role') === 'region' && v.el.getAttribute('aria-label') === 'Viewer' && v.el.hidden && !v.isOpen());
  v.setConversation(conv('advanced'));
  v.open('file:g1');
  await new Promise((r) => setTimeout(r, 0));
  check('open a file: the panel shows, onToggle(true), one preview call', v.isOpen() && eq(toggles, [true]) && eq(calls, ['g1']));
  const sel = v.el.querySelector('select');
  check('the select is grouped Files / Code', eq([...sel.querySelectorAll('optgroup')].map((g) => g.label), ['Files', 'Code']) && sel.value === 'file:g1');
  const ths = [...v.el.querySelectorAll('.viewer-grid thead th')].map((t) => t.textContent);
  check('sheets: column letters as the header, numbered data rows, tabs', eq(ths, ['', 'A', 'B', 'C']) && v.el.querySelectorAll('.viewer-grid tbody tr').length === 3 && v.el.querySelector('.viewer-grid tbody tr th').textContent === '1' && v.el.querySelectorAll('[role="tab"]').length === 2);
  check('sheet cells are text nodes', v.el.querySelector('.viewer-body b') === null && v.el.querySelector('.viewer-body').textContent.includes('<b>x</b>'));
  v.el.querySelectorAll('[role="tab"]')[1].click();
  check('a tab click shows that sheet', v.el.querySelector('[role="tab"][aria-selected="true"]').textContent === 'Summary' && v.el.querySelector('.viewer-grid tbody').textContent.includes('Checked'));
  check('a file shows Download (the file route), not Copy', !v.el.querySelector('.viewer-download').hidden && v.el.querySelector('.viewer-download').getAttribute('href') === '/chat/api/files/g1' && v.el.querySelector('.viewer-copy').hidden);

  v.open('file:up1');
  await new Promise((r) => setTimeout(r, 0));
  check('a refused preview (415): the fixed text and a Download link', v.el.querySelector('.viewer-error').textContent.includes(V.TEXT_NO_PREVIEW) && v.el.querySelector('.viewer-error a').getAttribute('href') === '/chat/api/files/up1');

  v.open({ key: 'file:d1', type: 'file', id: 'd1', name: 'r.docx' });
  await new Promise((r) => setTimeout(r, 0));
  check('document: heading, paragraph as text, ordered list, table', v.el.querySelector('.viewer-doc h1').textContent === 'Review' && v.el.querySelector('.viewer-doc img') === null && v.el.querySelectorAll('.viewer-doc ol li').length === 2 && v.el.querySelector('.viewer-doc table th').textContent === 'h1');

  const code = v.items().code[0];
  v.open(code);
  // the vendored highlight.js has no ABAP grammar: ABAP stays plain text, a known language is coloured
  check('code: <pre><code> (plain for a language highlight.js lacks), header with language and line count, Copy and Download', v.el.querySelector('.viewer-code code') !== null && v.el.querySelector('.viewer-code code').textContent === abap && v.el.querySelector('.viewer-meta').textContent === 'abap · 9 lines' && !v.el.querySelector('.viewer-copy').hidden && !v.el.querySelector('.viewer-download-code').hidden && v.el.querySelector('.viewer-download').hidden);
  v.open(v.items().code[1]);
  check('a known language (js) is highlighted', v.el.querySelector('.viewer-code code.hljs') !== null && v.el.querySelector('.viewer-meta').textContent === 'js · 1 line');
  check('open from a code block button ({code, lang}) finds the listed item', v.open({ code: `${abap}\n`, lang: 'abap' }) && v.current().key === code.key);

  v.close(true);
  check('close by the user: hidden, onToggle(false), flag set', !v.isOpen() && toggles[toggles.length - 1] === false && v.isClosedByUser());
  const c2 = conv('advanced');
  v.setConversation(c2);
  check('auto-open is blocked while closed by the user', v.autoOpen(c2.messages[1]) === false && !v.isOpen());
  v.noteSend();
  check('sending resets the flag; auto-open opens the turn\'s file', !v.isClosedByUser() && v.autoOpen(c2.messages[1]) === true && v.current().key === 'file:g1');
  v.el.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  check('Escape inside the panel closes it (as the user)', !v.isOpen() && v.isClosedByUser());
  v.noteSend();
  v.open('file:g1');
  v.setConversation({ id: 'other', access: 'basic', messages: [] });
  check('another conversation closes the panel without the flag', !v.isOpen() && !v.isClosedByUser());
  const nf = { id: 'c9', access: 'advanced', messages: [{ id: 'a', role: 'assistant', content: answer, parts: [{ type: 'text', text: answer }], status: 'done' }] };
  v.setConversation(nf);
  check('auto-open in advanced with a long code block and no file', v.autoOpen(nf.messages[0]) === true && v.current().name === 'zcl_order_check.clas.abap');
  v.close(false);
  check('parseCsv: a table, null when ragged', eq(V.parseCsv('a,b\n1,"x, y"\n'), [['a', 'b'], ['1', 'x, y']]) && V.parseCsv('a,b\n1\n') === null);
  v.el.remove();

  // the "Open" button: markdown option, and the message list passes it only in advanced
  const md = document.createElement('div');
  const opened = [];
  renderMarkdown(md, '```abap\nWRITE 1.\n```\n\n```mermaid\ngraph TD; A-->B\n```', { final: false, onOpenCode: (code, lang) => opened.push([code, lang]) });
  const openBtns = md.querySelectorAll('.codeblock-open');
  check('renderMarkdown with onOpenCode: one Open button per code block (not mermaid)', openBtns.length === 1);
  openBtns[0].click();
  check('the Open button hands over code and language', opened.length === 1 && opened[0][1] === 'abap' && opened[0][0].includes('WRITE 1.'));
  const md2 = document.createElement('div');
  renderMarkdown(md2, '```abap\nWRITE 1.\n```');
  check('without the option: no Open button', md2.querySelector('.codeblock-open') === null && md2.querySelector('.codeblock-copy') !== null);

  const log = document.createElement('section'); log.className = 'log';
  const col = document.createElement('div'); log.appendChild(col); document.body.appendChild(log);
  const files = [];
  const codes = [];
  const list = createMessageList(col, { onOpenFile: (f) => files.push(f.id), onOpenCode: (code, lang) => codes.push(lang) });
  list.render(conv('basic'), {});
  check('message list, basic: no Open button on code', col.querySelector('.codeblock-open') === null && col.querySelector('.codeblock') !== null);
  list.render(conv('advanced'), {});
  const advBtns = col.querySelectorAll('.codeblock-open');
  check('message list, advanced: an Open button per code block', advBtns.length === 2);
  advBtns[0].click();
  check('it calls onOpenCode', eq(codes, ['abap']));
  const card = col.querySelector('.msg.assistant .file-card.openable');
  card.querySelector('.file-name').click();
  card.querySelector('.file-tile').click();
  col.querySelector('.msg.user .file-card .file-name').dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Enter' }));
  check('file card name / icon (and Enter) open the file; the Download link is unchanged', eq(files, ['g1', 'g1', 'up1']) && card.querySelector('.file-download').getAttribute('href') === '/chat/api/files/g1');
  log.remove();
}

// ------------------------------------------- addendum (g): tools for personal agents
console.log('\n== tool sources (addendum g) ==');
{
  const catalog = {
    agents: [{ name: 'btp-admin', description: 'BTP', tier: 'basic' }, { name: 'entitlement-analyst', description: 'Ent', tier: 'basic' }],
    skills: [],
    tool_sources: [
      { name: 'btp-admin', description: 'The BTP account model', tier: 'basic', connectors: ['btp-mcp.cfapps.eu10.hana.ondemand.com', 'builtin:sapnotes'] },
      { name: 'platform-tickets', description: 'Jira and Confluence', tier: 'basic', connectors: ['builtin:jira', 'builtin:confluence'] },
      { name: 'abap-developer-deep', description: 'ARC-1', tier: 'advanced', connectors: ['arc1.example.hana.ondemand.com'] },
    ],
  };
  check('toolSources normalises the catalogue; an older server without tool_sources gives []', my.toolSources(catalog).length === 3 && eq(my.toolSources({ agents: [] }), []) && my.toolSources(catalog)[2].tier === 'advanced');
  check('toolSourceLabel: "name · label, label"', my.toolSourceLabel(catalog.tool_sources[0]) === 'btp-admin · btp-mcp.cfapps.eu10.hana.ondemand.com, builtin:sapnotes' && my.toolSourceLabel({ name: 'x', connectors: [] }) === 'x');
  const ok = { name: 'Quota watcher', description: '', instructions: 'Do', skills: [], consults: [], model: null };
  const advancedTools = my.advancedToolNames(catalog);
  check('validateAgent: at most 5 distinct tools, fixed text', my.validateAgent({ ...ok, tools: ['a', 'b', 'c', 'd', 'e', 'f'] }).tools === my.TEXTS.tools && my.validateAgent({ ...ok, tools: ['a', 'a'] }).tools === my.TEXTS.tools && !my.validateAgent({ ...ok, tools: ['a', 'b', 'c', 'd', 'e'] }).tools);
  check('validateAgent: an advanced tool from a non-developer is refused, from a developer fine', my.validateAgent({ ...ok, tools: ['abap-developer-deep'] }, { developer: false, advancedTools }).tools === my.TEXTS.tools_advanced && !my.validateAgent({ ...ok, tools: ['abap-developer-deep'] }, { developer: true, advancedTools }).tools);
  check('a server 422 at tools maps to the fixed server text', my.fieldErrors([{ loc: ['body', 'tools', '0'], msg: 'x' }], ['tools']).tools === my.SERVER_TEXTS.tools);
  const r = my.formatImportResult({ dropped: [{ item: 'agent:Quota watcher', field: 'tools', name: 'gone-source' }] });
  check('a dropped tool source is worded as such', r.dropped[0] === 'agent:Quota watcher: tool source gone-source does not exist here and was dropped');

  // The editor, under jsdom (no showModal there).
  const D = dom.window.HTMLDialogElement.prototype;
  if (!D.showModal) { D.showModal = function () { this.open = true; }; D.close = function () { this.open = false; this.dispatchEvent(new dom.window.Event('close')); }; }
  const { createAgentEditor } = await import('../static/chat/render/agentEditor.js');
  const notes = [];
  const saved = [];
  const editor = createAgentEditor({ notify: (t) => notes.push(t), onSaved: (k, row) => saved.push(row) });
  const dlg = () => document.querySelector('dialog.panel');
  const toolBoxes = () => [...dlg().querySelectorAll('.tools-group input[type=checkbox]')];
  const mine = { agents: [], skills: [] };

  editor.openAgent({ agent: null, catalog, mine, models: [], developer: false });
  let boxes = toolBoxes();
  const labels = [...dlg().querySelectorAll('.tools-group .check-name')].map((n) => n.firstChild.textContent);
  check('editor: the multi-select is built from tool_sources with the connector labels', boxes.length === 3 && labels[1] === 'platform-tickets · builtin:jira, builtin:confluence', JSON.stringify(labels));
  check('editor: the group is titled "Tools from admin agents"', dlg().querySelector('.tools-group legend').textContent.startsWith('Tools from admin agents'));
  const advRow = boxes[2].closest('label');
  check('editor, basic user: the advanced option carries the Advanced tag, is disabled and says why', boxes[2].disabled && advRow.querySelector('.badge.dev').textContent === 'Advanced' && advRow.querySelector('.check-hint').textContent === 'Advanced tools need the developer role' && boxes[2].getAttribute('aria-describedby') === advRow.querySelector('.check-hint').id);
  check('editor, basic user: the basic options are enabled', !boxes[0].disabled && !boxes[1].disabled);
  editor.close();

  editor.openAgent({ agent: { id: 'a1', name: 'Quota watcher', description: '', instructions: 'Do', skills: [], consults: [], tools: ['btp-admin', 'abap-developer-deep'], model: null }, catalog, mine, models: [], developer: true });
  boxes = toolBoxes();
  check('editor, developer: the advanced option is enabled, no hint', !boxes[2].disabled && !boxes[2].closest('label').querySelector('.check-hint'));
  check('editor: stored tools are loaded ticked on edit', boxes[0].checked && !boxes[1].checked && boxes[2].checked);
  // save sends tools
  let sent = null;
  let answer = { status: 200, body: {} };
  globalThis.fetch = async (url, init) => {
    sent = { url, init };
    const status = answer.status;
    return { ok: status < 300, status, headers: { get: () => 'application/json' }, json: async () => answer.body, text: async () => JSON.stringify(answer.body) };
  };
  answer = { status: 200, body: { id: 'a1', name: 'Quota watcher', tools: ['btp-admin', 'abap-developer-deep'] } };
  dlg().querySelector('form').dispatchEvent(new dom.window.Event('submit', { cancelable: true }));
  await new Promise((r) => setTimeout(r, 20));
  check('editor: save sends tools', sent && sent.init.method === 'PUT' && eq(JSON.parse(sent.init.body).tools, ['btp-admin', 'abap-developer-deep']), sent && sent.init.body);

  // the 6th tick is refused in the editor
  const six = { ...catalog, tool_sources: Array.from({ length: 6 }, (_, i) => ({ name: `src-${i}`, description: '', tier: 'basic', connectors: [`builtin:x${i}`] })) };
  editor.openAgent({ agent: null, catalog: six, mine, models: [], developer: true });
  boxes = toolBoxes();
  for (const b of boxes) { b.checked = true; b.dispatchEvent(new dom.window.Event('change')); }
  const err = dlg().querySelector('.tools-group .field-error');
  check('editor: a 6th tool source is refused with the fixed text', boxes.filter((b) => b.checked).length === 5 && !boxes[5].checked && !err.hidden && err.textContent === my.TEXTS.tools);
  editor.close();

  // a server 422 at tools is shown at the field
  editor.openAgent({ agent: null, catalog, mine, models: [], developer: true });
  dlg().querySelector('input[type=text]').value = 'Ticket bot';
  dlg().querySelector('textarea').value = 'Answer tickets.';
  toolBoxes()[1].checked = true;
  answer = { status: 422, body: { detail: [{ loc: ['body', 'tools', 0], msg: 'tools: not a tool source you can use', type: 'value_error' }] } };
  dlg().querySelector('form').dispatchEvent(new dom.window.Event('submit', { cancelable: true }));
  await new Promise((r) => setTimeout(r, 20));
  const terr = dlg().querySelector('.tools-group .field-error');
  check('editor: a server 422 at tools is shown at that field', sent.init.method === 'POST' && eq(JSON.parse(sent.init.body).tools, ['platform-tickets']) && !terr.hidden && terr.textContent === my.SERVER_TEXTS.tools && dlg().querySelector('.tools-group').classList.contains('invalid'));
  editor.close();

  // the lists: "n tools" badge on Mine, "Shares tools" on the admin list
  const { createAgentsPage } = await import('../static/chat/render/agents.js');
  globalThis.fetch = async (url) => {
    const body = url.includes('/catalog') ? catalog
      : url.includes('/my/agents') ? [
        { id: 'a1', name: 'Quota watcher', description: '', instructions: 'Do', skills: [], consults: [], tools: ['btp-admin', 'platform-tickets'], model: null, tier: 'basic' },
        { id: 'a2', name: 'Plain', description: '', instructions: 'Do', skills: [], consults: [], tools: [], model: null, tier: 'basic' },
        { id: 'a3', name: 'Single', description: '', instructions: 'Do', skills: [], consults: [], tools: ['btp-admin'], model: null, tier: 'basic' }]
        : [];
    return { ok: true, status: 200, headers: { get: () => 'application/json' }, json: async () => body };
  };
  const host = document.createElement('div');
  document.body.appendChild(host);
  const pageApi = createAgentsPage(host, { editor, notify: () => {}, onChat: () => {}, onChanged: () => {}, getModels: () => [], isDeveloper: () => false });
  await pageApi.load();
  const mineRows = [...host.querySelectorAll('.list-section.mine .row')];
  const badge = (row) => row.querySelector('.badge.tools')?.textContent || null;
  check('Mine list: a "n tools" badge only for an agent with tools', badge(mineRows[0]) === '2 tools' && badge(mineRows[1]) === null && badge(mineRows[2]) === '1 tool');
  const adminRows = [...host.querySelectorAll('.list-section.admin .row')];
  const tag = (row) => row.querySelector('.share-tag')?.textContent || null;
  check('admin list: "Shares tools" for a catalogue agent that is a tool source', tag(adminRows[0]) === 'Shares tools' && tag(adminRows[1]) === null, JSON.stringify(adminRows.map(tag)));
  host.remove();
}

// ---------------------------------------------------------------- dashboards (addendum i)
console.log('\n== dashboards (addendum i) ==');
{
  const C = await import('../static/chat/render/charts.js');
  const D = await import('../static/chat/render/dashboard.js');
  const months = ['2026-05', '2026-06', '2026-07', '2026-08', '2026-09', '2026-10'];
  const bar = { type: 'bar', title: 'Spend by service', x: 'Month', y: ['HANA', 'Integration', 'Runtime'], format: 'currency', unit: '€',
    data: { columns: ['Month', 'HANA', 'Integration', 'Runtime'], rows: months.map((m, i) => [m, 100 + i * 10, 50 + i, i === 2 ? null : 20]) } };

  // scales and ticks
  check('niceTicks: clean steps covering the domain', eq(C.niceTicks(0, 87), [0, 20, 40, 60, 80, 100]) && eq(C.niceTicks(0, 1), [0, 0.2, 0.4, 0.6, 0.8, 1]) && eq(C.niceTicks(-12, 30), [-20, -10, 0, 10, 20, 30]));
  check('niceTicks: a flat domain still gets an axis', C.niceTicks(5, 5).length >= 2 && C.niceTicks(0, 0).length >= 2);
  const s = C.scaleLinear([0, 100], [200, 0]);
  check('scaleLinear maps and inverts', s(0) === 200 && s(50) === 100 && s(100) === 0 && s.invert(100) === 50);
  check('formatValue by format and unit', C.formatValue(1234) === '1,234' && C.formatValue(12.5, 'percent') === '12.5%' && C.formatValue(1234, 'currency', '€') === '€ 1,234' && C.formatValue(1234, 'number', 'GB') === '1,234 GB' && C.formatValue(12900, 'number', '', { compact: true }) === '12.9K' && C.formatValue(null) === '–');

  // bar counts, grouped gaps, labels
  const gsvg = C.chartSvg(bar, { theme: 'light', width: 600 });
  const bars = [...gsvg.querySelectorAll('path.bar')];
  check('grouped bar: one bar per non-null value (6 x 3 - 1)', bars.length === 17, String(bars.length));
  check('bar colours: the light palette in fixed order', eq([0, 1, 2].map((j) => gsvg.querySelector(`path.bar[data-series="${j}"]`).getAttribute('fill')), C.CHART_PALETTE.light.slice(0, 3)));
  const dsvg = C.chartSvg(bar, { theme: 'dark', width: 600 });
  check('dark theme: the dark palette', dsvg.querySelector('path.bar[data-series="0"]').getAttribute('fill') === C.CHART_PALETTE.dark[0]);
  const xStart = (p) => Number(p.getAttribute('d').match(/^M([\d.]+),/)[1]);
  const first3 = bars.slice(0, 3).map(xStart);
  const w = Number(bars[0].getAttribute('d').match(/H([\d.]+)/)[1]) + 4 - xStart(bars[0]); // H runs to x + w - r
  check('grouped bars: a 2 px surface gap between neighbours, bars <= 24 px', Math.abs(first3[1] - first3[0] - (w + 2)) < 0.05 && w <= 24, `${first3} w=${w}`);
  check('more than 12 bars: no label on every bar', gsvg.querySelectorAll('.data-label').length === 0);
  const small = { ...bar, y: ['HANA'], data: { columns: bar.data.columns, rows: bar.data.rows } };
  check('<= 12 bars: a value label on every bar', C.chartSvg(small, { width: 600 }).querySelectorAll('.data-label').length === 6);
  check('aria-label summary', gsvg.getAttribute('role') === 'img' && gsvg.getAttribute('aria-label') === 'Bar chart: Spend by service, 6 categories, series HANA, Integration, Runtime');

  // stacked sums
  const stacked = { ...bar, stacked: true };
  const ssvg = C.chartSvg(stacked, { width: 600 });
  const segments = [...ssvg.querySelectorAll('path.bar')];
  const ends = segments.map((p) => Number(p.getAttribute('data-end')));
  const wanted = bar.data.rows.map((r) => r.slice(1).reduce((a, v) => a + (v || 0), 0));
  check('stacked: the last segment of each stack ends at the row sum', eq(wanted, [170, 181, 172, 203, 214, 225]) && wanted.every((t) => ends.includes(t)) && segments.length === 17);
  const yTicks = [...ssvg.querySelectorAll('.axis-y .tick')].map((t) => t.textContent);
  check('stacked: the y axis reaches the largest sum', yTicks[yTicks.length - 1] === '€ 250', yTicks.join('|'));

  // legend only for >= 2 series; the 9th series folds into Other
  check('legend for >= 2 series', C.chartView(bar).querySelectorAll('.chart-legend .legend-item').length === 3);
  check('no legend for one series', C.chartView(small).querySelector('.chart-legend') === null);
  const nine = Array.from({ length: 10 }, (_, i) => ({ name: `s${i}`, values: [i, null] }));
  const folded = C.foldSeries(nine);
  check('foldSeries: 8 kept, the rest summed into "Other"', folded.length === 9 && folded[8].name === 'Other' && folded[8].other === true && eq(folded[8].values, [17, null]) && eq(C.foldSeries(nine.slice(0, 8)).map((x) => x.name), nine.slice(0, 8).map((x) => x.name)));
  const wide = { type: 'line', title: 'Many', x: 'd', y: nine.slice(0, 9).map((x) => x.name), data: { columns: ['d', ...nine.slice(0, 9).map((x) => x.name)], rows: [['a', ...Array(9).fill(1)], ['b', ...Array(9).fill(2)]] } };
  const wv = C.chartView(wide);
  const names = [...wv.querySelectorAll('.legend-name')].map((n) => n.textContent);
  check('a chart with 9 series: legend ends with "Other" in the de-emphasis hue', names.length === 9 && names[8] === 'Other' && wv.querySelectorAll('.legend-item .swatch')[8].style.background !== '' && wv.querySelectorAll('path.line').length === 9);

  // donut slice labels >= 5%
  const pie = { type: 'pie', title: 'Share', x: 'k', y: ['v'], data: { columns: ['k', 'v'], rows: [['a', 50], ['b', 30], ['c', 15], ['d', 3], ['e', 2]] } };
  const psvg = C.chartSvg(pie, { width: 500 });
  check('donut: 5 slices, labels only on slices >= 5%', psvg.querySelectorAll('path.slice').length === 5 && eq([...psvg.querySelectorAll('.slice-label')].map((t) => t.textContent), ['50%', '30%', '15%']));
  check('donut: the total in the centre, aria-label', psvg.querySelector('.donut-total').textContent === '100' && psvg.getAttribute('aria-label') === 'Donut chart: Share, 5 slices');
  const manyPie = { ...pie, data: { columns: ['k', 'v'], rows: Array.from({ length: 11 }, (_, i) => [`k${i}`, 10]) } };
  check('donut: at most 8 slices, the rest is "Other"', C.slicesOf(manyPie).length === 8 && C.slicesOf(manyPie)[7].name === 'Other' && C.slicesOf(manyPie)[7].value === 40);

  // line/area/scatter
  const area = { type: 'area', title: 'Calls', x: 'Day', y: ['A', 'B'], stacked: true, data: { columns: ['Day', 'A', 'B'], rows: [['2026-10-03', 3, 4], ['2026-10-01', 1, 2], ['2026-10-02', 2, 3]] } };
  const asvg = C.chartSvg(area, { width: 600 });
  check('area: ISO dates sorted on a time axis, labelled YYYY-MM-DD', eq([...asvg.querySelectorAll('.axis-x .tick')].map((t) => t.textContent), ['2026-10-01', '2026-10-02', '2026-10-03']));
  check('area: fills and 2 px lines per series, end labels for <= 4 series', asvg.querySelectorAll('path.area-fill').length === 2 && asvg.querySelectorAll('path.line[stroke-width="2"]').length === 2 && asvg.querySelectorAll('.end-label').length >= 1);
  const sc = { type: 'scatter', title: 'Size vs cost', x: 'GB', y: ['Cost'], data: { columns: ['App', 'GB', 'Cost'], rows: [['a', 1, 10], ['b', 2, 18], ['c', 4, 33]] } };
  const scsvg = C.chartSvg(sc, { width: 500 });
  check('scatter: 8 px markers with a surface ring, 24 px hit areas', scsvg.querySelectorAll('circle.marker.ring[r="4"]').length === 3 && scsvg.querySelectorAll('circle.hit[r="12"]').length === 3);

  // tooltip content on hover, focusable marks
  const view = C.chartView(bar, { width: 600 });
  document.body.appendChild(view);
  const hit = view.querySelector('.hit[data-tip="1"]');
  hit.dispatchEvent(new window.Event('pointerover', { bubbles: true }));
  const tip = view.querySelector('.chart-tip');
  const rows = [...tip.querySelectorAll('.chart-tip-row')].map((r) => [r.querySelector('.chart-tip-value').textContent, r.querySelector('.chart-tip-name').textContent]);
  check('bar hover: one tooltip with the category and every series, the hovered one marked', !tip.hidden && tip.querySelector('.chart-tip-title').textContent === '2026-05' && eq(rows, [['€ 100', 'HANA'], ['€ 50', 'Integration'], ['€ 20', 'Runtime']]) && tip.querySelector('.chart-tip-row.current .chart-tip-name').textContent === 'Integration');
  check('the hovered bar lifts', view.querySelectorAll('path.bar.active').length === 1);
  check('one tooltip element per chart, inside the card', view.querySelectorAll('.chart-tip').length === 1 && tip.parentElement.classList.contains('chart-plot'));
  view.querySelector('svg').dispatchEvent(new window.Event('pointerleave'));
  check('pointer leaves: the tooltip hides', tip.hidden && view.querySelectorAll('path.bar.active').length === 0);
  const hit0 = view.querySelector('.hit[data-tip="0"]');
  check('marks are focusable with an accessible name', hit0.getAttribute('tabindex') === '0' && hit0.getAttribute('aria-label').startsWith('2026-05, HANA: € 100'));
  hit0.focus();
  check('focus shows the same tooltip', !tip.hidden && tip.querySelector('.chart-tip-row.current .chart-tip-name').textContent === 'HANA');
  hit0.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
  check('arrow keys step to the next mark', document.activeElement === view.querySelector('.hit[data-tip="1"]'));
  view.remove();
  const lv = C.chartView({ ...area, type: 'line', stacked: false }, { width: 600 });
  document.body.appendChild(lv);
  lv.querySelector('.hit[data-tip="2"]').dispatchEvent(new window.Event('pointerover', { bubbles: true }));
  const cross = lv.querySelector('.crosshair');
  check('line hover: crosshair shown at the point, tooltip lists every series', cross.getAttribute('visibility') === 'visible' && lv.querySelector('.chart-tip-title').textContent === '2026-10-03' && lv.querySelectorAll('.chart-tip-row').length === 2);
  lv.remove();

  // KPI tile
  const up = C.kpiTile({ type: 'kpi', title: 'Spend', value: 12900, unit: '€', delta: 4.2, delta_label: 'vs September', trend: [1, 3, 2, 5] });
  check('KPI: hero value compact with the unit, delta up and neutral without `better`, with the ▲ glyph and label', up.querySelector('.kpi-number').textContent === '12.9K' && up.querySelector('.kpi-unit').textContent === '€' && up.querySelector('.kpi-delta').classList.contains('up') && up.querySelector('.kpi-delta').classList.contains('neutral') && !up.querySelector('.kpi-delta').classList.contains('good') && !up.querySelector('.kpi-delta-value').style.color && up.querySelector('.kpi-arrow').textContent === '▲' && up.querySelector('.kpi-delta-value').textContent === '+4.2' && up.querySelector('.kpi-delta-label').textContent === 'vs September');
  check('KPI: a sparkline, one 2 px line', up.querySelectorAll('.kpi-spark path[stroke-width="2"]').length === 1);
  const down = C.kpiTile({ type: 'kpi', title: 'Errors', value: '3 open', delta: -2, unit: '%' });
  check('KPI: delta down and neutral without `better`, ▼, value as given, no sparkline', down.querySelector('.kpi-delta').classList.contains('down') && down.querySelector('.kpi-delta').classList.contains('neutral') && down.querySelector('.kpi-arrow').textContent === '▼' && down.querySelector('.kpi-delta-value').textContent === '−2%' && down.querySelector('.kpi-number').textContent === '3 open' && !down.querySelector('.kpi-spark'));

  const statusOf = (delta, better) => {
    const box = C.kpiTile({ type: 'kpi', title: 'T', value: 1, delta, better }).querySelector('.kpi-delta');
    return ['good', 'critical', 'neutral'].filter((c) => box.classList.contains(c)).join();
  };
  check('KPI: higher is better, a rise is good and a fall critical', statusOf(5, 'higher') === 'good' && statusOf(-5, 'higher') === 'critical');
  check('KPI: lower is better (spend, errors), a rise is critical and a fall good', statusOf(5, 'lower') === 'critical' && statusOf(-5, 'lower') === 'good');
  check('KPI: no change is neutral whichever way is better', statusOf(0, 'higher') === 'neutral' && statusOf(0, 'lower') === 'neutral');
  check('KPI: an unknown `better` is neutral', statusOf(5, 'sideways') === 'neutral');

  // dashboard body from a preview, table toggle
  const spec = { title: 'BTP spend <b>2026</b>', description: 'Six months.', widgets: [
    { type: 'kpi', title: 'Spend', value: 1234, unit: '€', delta: 3 },
    { ...bar, size: 2, note: 'Runtime has no data for July.' },
    pie,
    { type: 'table', title: 'Raw', data: { columns: ['a', 'b'], rows: [['<img src=x>', 1]] } },
  ] };
  const body = D.dashboardBody({ kind: 'dashboard', name: 'spend.dashboard.json', dashboard: spec }, { theme: 'light' });
  document.body.appendChild(body);
  check('dashboard body: title as text, description, one card per widget', body.querySelector('.dash-title').textContent === 'BTP spend <b>2026</b>' && !body.querySelector('.dash-title b') && body.querySelector('.dash-desc').textContent === 'Six months.' && body.querySelectorAll('.dash-card').length === 4);
  check('dashboard body: size 2 spans, KPI tile, chart, table widget, note', body.querySelector('.dash-card.size-2 svg.chart-svg') !== null && body.querySelector('.dash-kpi .kpi') !== null && body.querySelector('.dash-table td').textContent === '<img src=x>' && !body.querySelector('.dash-card img') && body.querySelector('.dash-note').textContent === 'Runtime has no data for July.');
  const card = body.querySelector('.dash-card.size-2');
  const toggle = card.querySelector('.dash-toggle');
  check('table toggle: only on chart cards', body.querySelectorAll('.dash-toggle').length === 2 && toggle.getAttribute('aria-pressed') === 'false');
  toggle.click();
  check('table toggle: swaps the chart for the data table', !card.querySelector('svg.chart-svg') && card.querySelectorAll('.dash-table tbody tr').length === 6 && card.querySelector('.dash-table td.num').textContent === '€ 100' && toggle.getAttribute('aria-pressed') === 'true' && toggle.textContent === 'Chart');
  toggle.click();
  check('table toggle: and back to the chart', card.querySelector('svg.chart-svg') !== null && toggle.textContent === 'Table');
  body.setTheme('dark');
  check('setTheme re-renders in the dark palette', card.querySelector('path.bar[data-series="0"]').getAttribute('fill') === C.CHART_PALETTE.dark[0]);
  body.remove();

  // the HTML export
  const html = D.dashboardHtml(spec);
  check('export: a standalone page with inline SVG and the tables, no <script>', html.startsWith('<!doctype html>') && html.includes('<svg') && html.includes('<table') && !/<script/i.test(html) && html.includes('<style>'));
  check('export: model text escaped, light palette', html.includes('<title>BTP spend &lt;b&gt;2026&lt;/b&gt;</title>') && !html.includes('<b>2026</b>') && !html.includes('<img src=x>') && html.includes(C.CHART_PALETTE.light[0]) && !html.includes(C.CHART_PALETTE.dark[0]));
  check('export: a file name from the title', D.dashboardFilename(spec) === 'BTP spend b 2026 b.html' && D.dashboardFilename({}) === 'dashboard.html');

  // the card label
  const dcard = fileCard({ id: 'd9', name: 'btp-spend.dashboard.json', media_type: D.DASHBOARD_MEDIA_TYPE, size: 2048, title: 'BTP spend <i>x</i>', widgets: 6 });
  check('file card: chart icon and "Dashboard · <title> · n widgets" as text', dcard.classList.contains('kind-chart') && dcard.querySelector('.file-name').textContent === 'Dashboard · BTP spend <i>x</i> · 6 widgets' && !dcard.querySelector('.file-name i') && dcard.querySelector('.file-sub').textContent === '2 KB' && dcard.querySelector('.file-download').getAttribute('download') === 'btp-spend.dashboard.json');
  const bare = fileCard({ id: 'd8', name: 'q3.dashboard.json', media_type: D.DASHBOARD_MEDIA_TYPE, size: 10, widgets: 1 });
  check('file card: without a title, the name without .dashboard.json', bare.querySelector('.file-name').textContent === 'Dashboard · q3 · 1 widget');

  let fm = newAssistantMessage();
  fm = applyEvent(fm, { type: 'file', data: { file_id: 'd9', name: 'btp-spend.dashboard.json', media_type: D.DASHBOARD_MEDIA_TYPE, size: 2048, title: 'BTP spend', widgets: 6 } });
  check('reducer: a dashboard file event keeps title and widgets', fm.parts[0].title === 'BTP spend' && fm.parts[0].widgets === 6);
  // the viewer: dashboard body, actions, title
  const V = await import('../static/chat/render/viewer.js');
  const api = { fileUrl: (id) => `/chat/api/files/${id}`, fetchFileBlob: async () => null, filePreview: async () => ({ kind: 'dashboard', name: 'btp-spend.dashboard.json', dashboard: spec }) };
  const v = V.createViewer({ api });
  document.body.appendChild(v.el);
  v.setConversation({ id: 'cd', access: 'basic', messages: [{ id: 'a', role: 'assistant', content: '', parts: [{ type: 'file', file_id: 'd9', name: 'btp-spend.dashboard.json', media_type: D.DASHBOARD_MEDIA_TYPE, size: 2048 }] }] });
  v.open('file:d9');
  await new Promise((r) => setTimeout(r, 0));
  const dl = v.el.querySelector('.viewer-download');
  const dh = v.el.querySelector('.viewer-download-html');
  check('viewer: the dashboard body', v.el.querySelector('.viewer-body .dash .dash-grid') !== null);
  check('viewer: title is the dashboard title, meta counts widgets', v.el.querySelector('.viewer-title').textContent === spec.title && v.el.querySelector('.viewer-meta').textContent.startsWith('Dashboard · 4 widgets'));
  check('viewer: "Download HTML" and "Download data" (the stored file)', !dh.hidden && !dh.disabled && dl.textContent === 'Download data' && dl.getAttribute('href') === '/chat/api/files/d9');
  let made = null;
  const origCreate = URL.createObjectURL;
  URL.createObjectURL = (b) => { made = b; return 'blob:x'; };
  let clicked = null;
  const origClick = window.HTMLAnchorElement.prototype.click;
  window.HTMLAnchorElement.prototype.click = function () { clicked = this.download; };
  dh.click();
  window.HTMLAnchorElement.prototype.click = origClick;
  URL.createObjectURL = origCreate;
  check('viewer: Download HTML saves <title>.html as text/html', clicked === 'BTP spend b 2026 b.html' && made && made.type.startsWith('text/html'));
  v.el.remove();
}

console.log(failed ? `\n${failed} FAILED` : '\nall passed');
process.exit(failed ? 1 : 0);
