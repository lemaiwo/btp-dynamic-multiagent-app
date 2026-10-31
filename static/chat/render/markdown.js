// Markdown -> sanitized DOM. marked + DOMPurify + highlight.js are the
// vendored classic scripts (globals); mermaid is loaded on first use.
// Nothing the model wrote reaches innerHTML without DOMPurify.

import { effectiveTheme, onThemeChange } from '../theme.js';

const SANITIZE = {
  FORBID_TAGS: ['style', 'form', 'input', 'button', 'select', 'textarea', 'iframe', 'object', 'embed'],
  FORBID_ATTR: ['style'],
  ADD_ATTR: ['target'],
};

let hooked = false;
function ensureHooks(purify) {
  if (hooked || !purify?.addHook) return;
  hooked = true;
  purify.addHook('afterSanitizeAttributes', (node) => {
    if (node.tagName === 'A' && node.hasAttribute('href')) {
      node.setAttribute('target', '_blank');
      node.setAttribute('rel', 'noopener noreferrer');
    }
  });
}

export function libs() {
  const g = globalThis;
  return { marked: g.marked, DOMPurify: g.DOMPurify, hljs: g.hljs };
}

/** Markdown string -> sanitized HTML string. */
export function toSafeHtml(markdown) {
  const { marked, DOMPurify } = libs();
  if (!marked || !DOMPurify) throw new Error('markdown libraries are not loaded');
  ensureHooks(DOMPurify);
  const html = marked.parse(String(markdown ?? ''), { gfm: true, breaks: false });
  return DOMPurify.sanitize(html, SANITIZE);
}

/**
 * Render markdown into `container` (replacing its children): code blocks get a
 * header with language + copy button and highlight.js colours; `final` also
 * renders mermaid fences (async) and auto-highlights unknown languages.
 */
export function renderMarkdown(container, markdown, { final = false } = {}) {
  container.innerHTML = toSafeHtml(markdown);
  decorateCode(container, final);
  if (final) renderMermaid(container);
  return container;
}

function languageOf(code) {
  const m = /\blanguage-([\w+#.-]+)/.exec(code.className || '');
  return m ? m[1].toLowerCase() : '';
}

function decorateCode(container, final) {
  const { hljs } = libs();
  for (const code of container.querySelectorAll('pre > code')) {
    const pre = code.parentElement;
    const lang = languageOf(code);
    if (lang === 'mermaid') { pre.classList.add('mermaid-source'); continue; }
    if (hljs) {
      try {
        if (lang && hljs.getLanguage(lang)) {
          code.innerHTML = hljs.highlight(code.textContent, { language: lang, ignoreIllegals: true }).value;
          code.classList.add('hljs');
        } else if (final && !lang && code.textContent.length < 20000) {
          code.innerHTML = hljs.highlightAuto(code.textContent).value;
          code.classList.add('hljs');
        }
      } catch { /* leave plain */ }
    }
    const wrap = document.createElement('div');
    wrap.className = 'codeblock';
    const head = document.createElement('div');
    head.className = 'codeblock-head';
    const label = document.createElement('span');
    label.className = 'codeblock-lang';
    label.textContent = lang || 'text';
    const copy = document.createElement('button');
    copy.type = 'button';
    copy.className = 'codeblock-copy';
    copy.textContent = 'Copy';
    copy.setAttribute('aria-label', 'Copy code');
    copy.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(code.textContent);
        copy.textContent = 'Copied';
      } catch {
        copy.textContent = 'Copy failed';
      }
      setTimeout(() => { copy.textContent = 'Copy'; }, 1500);
    });
    head.append(label, copy);
    pre.replaceWith(wrap);
    wrap.append(head, pre);
  }
}

let mermaidLoading = null;
function loadMermaid() {
  if (globalThis.mermaid) return Promise.resolve(globalThis.mermaid);
  if (mermaidLoading) return mermaidLoading;
  mermaidLoading = new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = '/static/vendor/mermaid.min.js';
    s.onload = () => {
      if (!globalThis.mermaid) { mermaidLoading = null; reject(new Error('mermaid missing')); return; }
      resolve(globalThis.mermaid);
    };
    s.onerror = () => { mermaidLoading = null; reject(new Error('mermaid failed to load')); };
    document.head.appendChild(s);
  });
  return mermaidLoading;
}

let mermaidSeq = 0;
let mermaidTheme = null;
async function renderMermaid(container) {
  const fences = container.querySelectorAll('pre.mermaid-source > code');
  if (!fences.length) return;
  const { DOMPurify } = libs();
  let mermaid;
  try { mermaid = await loadMermaid(); } catch { return; }
  const theme = effectiveTheme() === 'dark' ? 'dark' : 'default';
  if (mermaidTheme !== theme) {
    mermaid.initialize({
      startOnLoad: false, securityLevel: 'strict', theme,
      htmlLabels: false, flowchart: { htmlLabels: false },
      suppressErrorRendering: true,
    });
    mermaidTheme = theme;
  }
  for (const code of fences) {
    const pre = code.parentElement;
    if (!pre || !pre.isConnected) continue;
    const holder = document.createElement('div');
    holder.className = 'mermaid-diagram';
    holder.dataset.source = code.textContent;
    if (await drawInto(mermaid, DOMPurify, holder)) {
      pre.replaceWith(holder);
      // the diagram is taller than the fence it replaces: let the list re-stick
      holder.dispatchEvent(new CustomEvent('chat:layout', { bubbles: true }));
    } else {
      const note = document.createElement('p');
      note.className = 'mermaid-error';
      note.textContent = 'Diagram could not be rendered; the source is shown instead.';
      pre.after(note);
    }
  }
}

async function drawInto(mermaid, DOMPurify, holder) {
  try {
    const { svg } = await mermaid.render(`mmd-${Date.now().toString(36)}-${mermaidSeq++}`, holder.dataset.source);
    holder.innerHTML = DOMPurify.sanitize(svg, { USE_PROFILES: { svg: true, svgFilters: true } });
    return true;
  } catch {
    return false;
  }
}

// A theme switch redraws the diagrams on the page in the matching theme.
onThemeChange(async () => {
  const holders = document.querySelectorAll('.mermaid-diagram[data-source]');
  if (!holders.length || !globalThis.mermaid) return;
  const theme = effectiveTheme() === 'dark' ? 'dark' : 'default';
  globalThis.mermaid.initialize({
    startOnLoad: false, securityLevel: 'strict', theme,
    htmlLabels: false, flowchart: { htmlLabels: false }, suppressErrorRendering: true,
  });
  mermaidTheme = theme;
  for (const h of holders) await drawInto(globalThis.mermaid, libs().DOMPurify, h);
});
