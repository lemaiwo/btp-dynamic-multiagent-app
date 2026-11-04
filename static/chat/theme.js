// Light / dark: prefers-color-scheme by default, a toggle persisted in
// localStorage. The page's own colours are CSS variables; the two vendored
// highlight.js themes are <link>s switched by their `media` attribute.

const KEY = 'chat.theme';
const media = typeof matchMedia === 'function' ? matchMedia('(prefers-color-scheme: dark)') : null;
const listeners = new Set();

export function storedPreference() {
  try { return localStorage.getItem(KEY) || null; } catch { return null; }
}

export function effectiveTheme() {
  const pref = storedPreference();
  if (pref === 'light' || pref === 'dark') return pref;
  return media && media.matches ? 'dark' : 'light';
}

function apply() {
  const theme = effectiveTheme();
  document.documentElement.dataset.theme = theme;
  const light = document.getElementById('hljs-light');
  const dark = document.getElementById('hljs-dark');
  if (light) light.media = theme === 'light' ? 'all' : 'not all';
  if (dark) dark.media = theme === 'dark' ? 'all' : 'not all';
  for (const fn of listeners) fn(theme);
  return theme;
}

export function initTheme() {
  if (media) media.addEventListener('change', () => { if (!storedPreference()) apply(); });
  return apply();
}

export function toggleTheme() {
  const next = effectiveTheme() === 'dark' ? 'light' : 'dark';
  try { localStorage.setItem(KEY, next); } catch { /* private mode */ }
  return apply();
}

export function onThemeChange(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}
