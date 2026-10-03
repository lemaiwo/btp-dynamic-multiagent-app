# Vendored browser libraries

Downloaded or copied, not built. The workbench must work with no CDN access.
These files are loaded on demand with a `<script>` tag and are excluded from
`Component-preload.js` (see `ui5.yaml`).

| File | Source | Version | Licence |
| --- | --- | --- | --- |
| `marked.min.js` | copied from `ui5-admin/webapp/vendor/` (https://cdn.jsdelivr.net/npm/marked@15/marked.min.js) | 15.0.12 | MIT |
| `purify.min.js` | copied from `ui5-admin/webapp/vendor/` (https://cdn.jsdelivr.net/npm/dompurify@3/dist/purify.min.js) | 3.4.14 | Apache-2.0 or MPL-2.0 |
| `diff.min.js` | jsdiff UMD build, `node_modules/diff/dist/diff.min.js` | 5.2.0 | BSD-3-Clause |

To refresh `diff.min.js`: bump the pinned `diff` devDependency in
`package.json`, `npm install`, then `npm run vendor:diff`.
