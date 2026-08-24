/**
 * Behavioural tests for the run-report rendering pipeline.
 *
 * tests/test_report_render.mjs stays dependency-free and can only assert that
 * the sanitize call is *present* in the render path. This suite asserts what it
 * actually *does*, by running the real vendored bundles against a real DOM.
 *
 * Requires jsdom:  npm install
 * Run:             node tests/test_report_sanitize.mjs
 *
 * Not covered here: Mermaid. Its UMD bundle assigns `globalThis.mermaid` from a
 * `var` that does not survive Node/jsdom module scope, and it needs layout APIs
 * (getBBox) jsdom lacks. Diagram rendering is verifiable only in a real browser
 * — see the manual verification step in the design doc.
 */
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);

let JSDOM;
try {
  ({ JSDOM } = require('jsdom'));
} catch {
  console.error('\nFAIL: jsdom is not installed.\n');
  console.error('This suite tests that report sanitization actually strips injected');
  console.error('content. It is skipped by no one — a skipped security test reads the');
  console.error('same as a passing one. Install it and re-run:\n');
  console.error('    npm install\n');
  process.exit(1);
}

const { marked } = require('../static/vendor/marked.min.js');
const createDOMPurify = require('../static/vendor/purify.min.js');

let failed = 0;
const check = (label, cond, detail = '') => {
  if (cond) { console.log(`  PASS  ${label}`); }
  else { failed++; console.log(`  FAIL  ${label}   ${detail}`); }
};

// Mirror templates/admin.html renderReportBody(): marked -> DOMPurify -> DOM.
// The options MUST stay in sync with that function; if they drift, this suite
// stops describing what the admin UI does.
const SANITIZE_OPTS = {
  FORBID_TAGS: ['style', 'form', 'input', 'button', 'select', 'textarea'],
  FORBID_ATTR: ['style'],
};

function render(bodyMd) {
  const window = new JSDOM('<!doctype html><body></body>').window;
  const DOMPurify = createDOMPurify(window);
  if (!DOMPurify.isSupported) {
    throw new Error('DOMPurify reports isSupported=false — it would no-op silently');
  }
  const html = DOMPurify.sanitize(marked.parse(bodyMd, { gfm: true }), SANITIZE_OPTS);
  const out = new JSDOM('<!doctype html><body></body>').window.document;
  out.body.innerHTML = html;
  return out;
}

console.log('\n== the sanitizer is actually running ==');
{
  const window = new JSDOM('<!doctype html><body></body>').window;
  const DOMPurify = createDOMPurify(window);
  // If this is ever false, DOMPurify.sanitize returns its input unchanged and
  // every other assertion below would pass while sanitizing nothing.
  check('DOMPurify.isSupported', DOMPurify.isSupported === true);
}

console.log('\n== injected content is stripped ==');
{
  const d = render([
    '<script>window.__pwned = 1</script>',
    '',
    '<img src=x onerror="window.__pwned = 1">',
    '',
    '[a link](javascript:window.__pwned=1)',
  ].join('\n'));

  check('no <script> survives', d.querySelectorAll('script').length === 0);
  const img = d.querySelector('img');
  check('<img> itself survives', !!img);
  check('onerror attribute is stripped', img ? !img.hasAttribute('onerror') : false);
  const a = d.querySelector('a');
  check('javascript: href is removed',
        !a || !a.hasAttribute('href'),
        a ? String(a.getAttribute('href')) : 'no anchor');
}

console.log('\n== the FORBID_TAGS hardening applies ==');
{
  // Not XSS, but a UI-redress / credential-phishing primitive inside an
  // admin-scoped page. These must not survive.
  const d = render([
    '<style>body { display: none }</style>',
    '',
    '<form><input name="password"><button>Go</button></form>',
    '',
    '<textarea>x</textarea>',
  ].join('\n'));

  for (const tag of ['style', 'form', 'input', 'button', 'textarea']) {
    check(`<${tag}> is removed`, d.querySelectorAll(tag).length === 0);
  }
}

console.log('\n== legitimate report content survives ==');
{
  const d = render([
    '# Heading',
    '',
    '| Source | Findings |',
    '| --- | --- |',
    '| ADT | 3 |',
    '| CDS | 0 |',
    '',
    '```mermaid',
    'pie title Findings',
    '  "ADT" : 3',
    '```',
    '',
    '[safe link](https://example.com/x)',
  ].join('\n'));

  check('heading renders', d.querySelectorAll('h1').length === 1);
  check('table renders', d.querySelectorAll('table').length === 1);
  check('table keeps all rows', d.querySelectorAll('tr').length === 3,
        String(d.querySelectorAll('tr').length));
  // The renderer locates fences with this exact selector, so sanitization must
  // not disturb the class — otherwise diagrams silently stop being found.
  check('mermaid fence still findable by the renderer selector',
        d.querySelectorAll('pre > code.language-mermaid').length === 1);
  const a = d.querySelector('a');
  check('https link keeps its href', !!(a && a.getAttribute('href')));
}

console.log(failed ? `\n${failed} check(s) FAILED\n` : '\nAll checks passed\n');
process.exit(failed ? 1 : 0);
