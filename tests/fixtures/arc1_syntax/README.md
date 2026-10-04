# ARC-1 syntax dry-run payloads (synthetic)

Every file here is **synthetic, replace with the real payload from L1**: the
real answer of `SAPDiagnose action=syntax` with a `source` argument has not
been recorded yet (plan assumption A5).

- `*.json`: `{"_note": ..., "payload": <the tool text as JSON>}`; the tests
  send `json.dumps(payload)` as the ARC-1 answer.
- `*.txt`: a `# note` line, a `---` line, then the tool text verbatim.

`tests/test_ide_syntax_check.py` pins the status each one must parse to;
`unknown_shape`, `error_object` and `plain_text_ok` must stay `unavailable`.
