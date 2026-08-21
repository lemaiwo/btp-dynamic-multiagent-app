// Markdown rendering contract for run reports.
// Run:  node tests/test_report_render.mjs
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { marked } = require('../static/vendor/marked.min.js');

let failed = 0;
const check = (label, cond, detail = '') => {
  if (cond) { console.log(`  PASS  ${label}`); }
  else { failed++; console.log(`  FAIL  ${label}   ${detail}`); }
};

console.log('\n== markdown -> html ==');
const html = marked.parse(
  '| a | b |\n| --- | --- |\n| 1 | 2 |\n\n' +
  '```mermaid\npie title T\n  "x" : 1\n```\n',
  { gfm: true },
);
check('table renders', html.includes('<table>'), html.slice(0, 120));
check('mermaid fence keeps its language class', html.includes('language-mermaid'));
check('fence content is left verbatim', html.includes('pie title T'));

console.log('\n== sanitization is wired in ==');
// Structural, not behavioural: DOMPurify needs a DOM, so this guards against
// the realistic regression (someone drops the sanitize call) rather than
// proving sanitization works. Behaviour is covered by the manual step in the spec.
const tpl = readFileSync(new URL('../templates/admin.html', import.meta.url), 'utf8');
const i = tpl.indexOf('function renderReportBody');
check('renderReportBody exists', i !== -1);
const render = tpl.slice(i, i + 4000);
check('markdown output is sanitized', /DOMPurify\.sanitize/.test(render));
check('never inserts unsanitized markdown',
      !/innerHTML\s*=\s*marked\.parse/.test(render));
check('mermaid svg is sanitized too',
      (render.match(/DOMPurify\.sanitize/g) || []).length >= 2);

process.exit(failed ? 1 : 0);
