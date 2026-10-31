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
const { applyEvent, applyStreamEvent, newAssistantMessage, activitySummary } = await import('../static/chat/state.js');
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
