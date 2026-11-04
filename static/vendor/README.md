# Vendored browser libraries

Downloaded, not built. The admin UI and the web chat (`static/chat/`) must work with no CDN access.

| File | Source | Version |
| --- | --- | --- |
| `marked.min.js` | https://cdn.jsdelivr.net/npm/marked@15/marked.min.js | 15.x |
| `purify.min.js` | https://cdn.jsdelivr.net/npm/dompurify@3/dist/purify.min.js | 3.x |
| `mermaid.min.js` | https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js | 11.x |
| `highlight.min.js` | https://cdn.jsdelivr.net/npm/@highlightjs/cdn-assets@11/highlight.min.js (core + common languages) | 11.12.0 |
| `highlight-github.min.css` | https://cdn.jsdelivr.net/npm/@highlightjs/cdn-assets@11/styles/github.min.css (light theme) | 11.12.0 |
| `highlight-github-dark.min.css` | https://cdn.jsdelivr.net/npm/@highlightjs/cdn-assets@11/styles/github-dark.min.css (dark theme) | 11.12.0 |

To refresh, re-run the curl commands in
`docs/superpowers/plans/2026-08-21-generic-markdown-run-reports.md` Task 4
and re-run `node tests/test_report_render.mjs`.

The web chat (`templates/webchat.html`) loads marked, DOMPurify and
highlight.js as classic scripts and mermaid on first use; `node
tests/test_webchat_ui.mjs` exercises the first three.
