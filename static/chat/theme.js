// Themes. A theme is an id (stored in localStorage under `chat.theme`) with a
// MODE, light or dark. `system` (the default, nothing stored) follows
// prefers-color-scheme. The page's colours are CSS variables: the mode is
// `data-theme` on <html> (the base light / dark set, and what charts,
// mermaid and the two vendored highlight.js themes follow), the theme itself
// is `data-skin` (absent for system / light / dark), whose block in
// styles.css overrides the variables. Listeners of onThemeChange get the
// MODE and are called only when it changes; onSkinChange gets the theme id.

const KEY = 'chat.theme';
const media = typeof matchMedia === 'function' ? matchMedia('(prefers-color-scheme: dark)') : null;
const listeners = new Set();
const skinListeners = new Set();

// The offered themes, in menu order. The SAP ones approximate the SAP theme
// of that name (colours of its theme parameters); they do not load SAP's CSS.
export const THEMES = Object.freeze([
  { id: 'system', label: 'System', group: 'Standard', mode: null },
  { id: 'light', label: 'Light', group: 'Standard', mode: 'light' },
  { id: 'dark', label: 'Dark', group: 'Standard', mode: 'dark' },
  { id: 'horizon', label: 'Morning Horizon', group: 'SAP Fiori', mode: 'light' },
  { id: 'horizon-dark', label: 'Evening Horizon', group: 'SAP Fiori', mode: 'dark' },
  { id: 'horizon-hcb', label: 'Horizon High Contrast Black', group: 'SAP Fiori', mode: 'dark' },
  { id: 'horizon-hcw', label: 'Horizon High Contrast White', group: 'SAP Fiori', mode: 'light' },
  { id: 'quartz', label: 'Quartz Light', group: 'SAP Fiori', mode: 'light' },
  { id: 'quartz-dark', label: 'Quartz Dark', group: 'SAP Fiori', mode: 'dark' },
  { id: 'belize', label: 'Belize', group: 'SAP Fiori', mode: 'light' },
  { id: 'sapgui', label: 'SAP GUI classic', group: 'Classic', mode: 'light' },
]);
const BY_ID = new Map(THEMES.map((t) => [t.id, t]));

export function storedPreference() {
  let v = null;
  try { v = localStorage.getItem(KEY) || null; } catch { return null; }
  return v && BY_ID.has(v) && v !== 'system' ? v : null;
}

/** The theme id in effect: the stored one, else `system`. */
export function currentTheme() {
  return storedPreference() || 'system';
}

/** The MODE in effect: `light` or `dark`. */
export function effectiveTheme() {
  const t = BY_ID.get(currentTheme());
  if (t && t.mode) return t.mode;
  return media && media.matches ? 'dark' : 'light';
}

let lastMode = null;
function apply() {
  const mode = effectiveTheme();
  const id = currentTheme();
  const root = document.documentElement;
  root.dataset.theme = mode;
  if (id === 'system' || id === 'light' || id === 'dark') delete root.dataset.skin;
  else root.dataset.skin = id;
  const light = document.getElementById('hljs-light');
  const dark = document.getElementById('hljs-dark');
  if (light) light.media = mode === 'light' ? 'all' : 'not all';
  if (dark) dark.media = mode === 'dark' ? 'all' : 'not all';
  const modeChanged = lastMode !== null && lastMode !== mode;
  lastMode = mode;
  if (modeChanged) for (const fn of listeners) fn(mode);
  for (const fn of skinListeners) fn(id, mode);
  return mode;
}

export function initTheme() {
  if (media) media.addEventListener('change', () => { if (!storedPreference()) apply(); });
  return apply();
}

/** Switch to a theme by id; an unknown id is ignored. Answers the mode. */
export function setTheme(id) {
  if (!BY_ID.has(id)) return effectiveTheme();
  try {
    if (id === 'system') localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, id);
  } catch { /* private mode */ }
  return apply();
}

/** Light <-> dark, kept for callers of the old toggle. */
export function toggleTheme() {
  return setTheme(effectiveTheme() === 'dark' ? 'light' : 'dark');
}

export function onThemeChange(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function onSkinChange(fn) {
  skinListeners.add(fn);
  return () => skinListeners.delete(fn);
}
