// Pure helpers shared by the UI and the jsdom tests. No DOM, no fetch.

const KIB = 1024;

/** 1234 -> "1.2 KB"; 0 -> "0 B". Base 1024, one decimal under 10. */
export function formatSize(bytes) {
  const n = Number(bytes);
  if (!Number.isFinite(n) || n < 0) return '';
  if (n < KIB) return `${Math.round(n)} B`;
  const units = ['KB', 'MB', 'GB'];
  let v = n / KIB;
  let i = 0;
  while (v >= KIB && i < units.length - 1) { v /= KIB; i++; }
  const text = v < 10 ? v.toFixed(1).replace(/\.0$/, '') : String(Math.round(v));
  return `${text} ${units[i]}`;
}

/** "12s", "1m 05s", "0s" from milliseconds. */
export function formatDuration(ms) {
  const s = Math.max(0, Math.round(Number(ms || 0) / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rest = s % 60;
  return `${m}m ${String(rest).padStart(2, '0')}s`;
}

export const DATE_GROUPS = ['Today', 'Yesterday', 'Previous 7 days', 'Older'];

function startOfDay(d) {
  const x = new Date(d);
  x.setHours(0, 0, 0, 0);
  return x;
}

/** Which of DATE_GROUPS an ISO timestamp falls in, relative to `now`. */
export function dateGroup(iso, now = new Date()) {
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return 'Older';
  const today = startOfDay(now);
  const day = startOfDay(t);
  const diffDays = Math.round((today - day) / 86400000);
  if (diffDays <= 0) return 'Today';
  if (diffDays === 1) return 'Yesterday';
  if (diffDays <= 7) return 'Previous 7 days';
  return 'Older';
}

/**
 * Group conversations by `updated_at`, newest first inside each group,
 * groups in DATE_GROUPS order, empty groups left out.
 * -> [{label, items}]
 */
export function groupConversations(list, now = new Date()) {
  const buckets = new Map(DATE_GROUPS.map((g) => [g, []]));
  const sorted = [...(list || [])].sort(
    (a, b) => new Date(b.updated_at) - new Date(a.updated_at),
  );
  for (const c of sorted) buckets.get(dateGroup(c.updated_at, now)).push(c);
  return DATE_GROUPS.filter((g) => buckets.get(g).length)
    .map((g) => ({ label: g, items: buckets.get(g) }));
}

/** Case-insensitive title filter; blank query keeps everything. */
export function filterConversations(list, query) {
  const q = (query || '').trim().toLowerCase();
  if (!q) return list || [];
  return (list || []).filter((c) => (c.title || '').toLowerCase().includes(q));
}

// Media types by extension, the client-side half of the server's sniffing.
export const EXTENSION_TYPES = {
  png: 'image/png',
  jpg: 'image/jpeg',
  jpeg: 'image/jpeg',
  pdf: 'application/pdf',
  txt: 'text/plain',
  md: 'text/markdown',
  csv: 'text/csv',
  json: 'application/json',
  docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
};

export const EXTENSIONS_BY_TYPE = Object.fromEntries(
  Object.entries(EXTENSION_TYPES).map(([ext, type]) => [type, ext]),
);

/** The media type the client believes a File has: extension first, then File.type. */
export function mediaTypeOf(file) {
  const name = (file && file.name) || '';
  const ext = name.includes('.') ? name.split('.').pop().toLowerCase() : '';
  if (ext && EXTENSION_TYPES[ext]) return EXTENSION_TYPES[ext];
  return (file && file.type) || '';
}

export const LIMIT_TEXTS = {
  too_many: 'Too many files for one message.',
  too_large: 'File is too large.',
  type: 'File type not accepted.',
  empty: 'File is empty.',
};

/**
 * Check files against the /me limits before they become chips.
 * `existing` are already-attached files, `incoming` the new ones.
 * -> {accepted: File[], rejected: [{file, reason, text}]}
 */
export function checkAttachments(existing, incoming, limits) {
  const maxFiles = Number(limits?.max_files) || 5;
  const maxBytes = Number(limits?.max_file_bytes) || 10 * 1024 * 1024;
  const allowed = Array.isArray(limits?.media_types) && limits.media_types.length
    ? new Set(limits.media_types)
    : null;
  const accepted = [];
  const rejected = [];
  let count = (existing || []).length;
  for (const file of incoming || []) {
    let reason = null;
    const type = mediaTypeOf(file);
    if (count >= maxFiles) reason = 'too_many';
    else if (!file.size) reason = 'empty';
    else if (file.size > maxBytes) reason = 'too_large';
    else if (allowed && !allowed.has(type)) reason = 'type';
    if (reason) {
      rejected.push({ file, reason, text: LIMIT_TEXTS[reason] });
    } else {
      accepted.push(file);
      count++;
    }
  }
  return { accepted, rejected };
}

/** A fixed, short description of a set of refusals for one toast. */
export function describeRejections(rejected, limits) {
  if (!rejected.length) return '';
  const reasons = new Set(rejected.map((r) => r.reason));
  const parts = [];
  if (reasons.has('too_many')) parts.push(`At most ${limits?.max_files || 5} files per message.`);
  if (reasons.has('too_large')) parts.push(`Files must be under ${formatSize(limits?.max_file_bytes || 10 * 1024 * 1024)}.`);
  if (reasons.has('type')) parts.push('Accepted: PNG, JPEG, PDF, text, Markdown, CSV, JSON, Word and Excel.');
  if (reasons.has('empty')) parts.push('Empty files are left out.');
  const n = rejected.length;
  return `${n === 1 ? '1 file was' : `${n} files were`} left out. ${parts.join(' ')}`;
}

/** The kind a file card's icon shows. */
export function fileKind(mediaType, name = '') {
  const t = (mediaType || '').toLowerCase();
  const ext = name.includes('.') ? name.split('.').pop().toLowerCase() : '';
  if (t.startsWith('image/')) return 'image';
  if (t === 'application/pdf' || ext === 'pdf') return 'pdf';
  if (t.includes('spreadsheet') || ext === 'xlsx' || t === 'text/csv' || ext === 'csv') return 'sheet';
  if (t.includes('wordprocessing') || ext === 'docx') return 'doc';
  if (t === 'application/json' || ext === 'json') return 'code';
  if (t.startsWith('text/') || ext === 'txt' || ext === 'md') return 'text';
  return 'file';
}

export function isImageType(mediaType) {
  return mediaType === 'image/png' || mediaType === 'image/jpeg';
}

/** First non-empty line of a text, cut at `max` characters. */
export function titleFromText(text, max = 120) {
  const line = (text || '').split(/\r?\n/).map((l) => l.trim()).find(Boolean) || 'New chat';
  return line.length > max ? `${line.slice(0, max - 1)}…` : line;
}

export function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}
