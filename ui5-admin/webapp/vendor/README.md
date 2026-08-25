# Vendored browser libraries

Downloaded, not built. The admin UI must work with no CDN access.

| File | Source | Version |
| --- | --- | --- |
| `marked.min.js` | https://cdn.jsdelivr.net/npm/marked@15/marked.min.js | 15.x |
| `purify.min.js` | https://cdn.jsdelivr.net/npm/dompurify@3/dist/purify.min.js | 3.x |
| `mermaid.min.js` | https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js | 11.x |

To refresh, re-run the curl commands in
`docs/superpowers/plans/2026-08-21-generic-markdown-run-reports.md` Task 4
and re-run `node tests/test_report_render.mjs`.
