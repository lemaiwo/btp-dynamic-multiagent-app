// The composer: auto-growing textarea (Enter sends, Shift+Enter newline),
// paperclip, drag-and-drop over the main pane, pasted images, chips with
// name / size / remove, client-side limit checks, Stop while a run is live.

import { checkAttachments, describeRejections, formatSize, fileKind, isImageType, EXTENSION_TYPES } from './util.js';

function svg(d, size = 20) {
  const s = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  s.setAttribute('viewBox', '0 0 24 24');
  s.setAttribute('width', size);
  s.setAttribute('height', size);
  s.setAttribute('aria-hidden', 'true');
  const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  p.setAttribute('d', d);
  s.appendChild(p);
  return s;
}

const MAX_TEXT = 64 * 1024;

export function createComposer(root, { getLimits, onSend, onStop, notify, dropZone }) {
  let files = [];
  let running = false;
  let stopping = false;
  let disabled = false;
  const urls = new Map(); // File -> object URL (image chips)

  const form = document.createElement('form');
  form.className = 'composer';
  form.noValidate = true;

  const chips = document.createElement('div');
  chips.className = 'chips';
  chips.hidden = true;

  const row = document.createElement('div');
  row.className = 'composer-row';

  const picker = document.createElement('input');
  picker.type = 'file';
  picker.multiple = true;
  picker.hidden = true;
  picker.accept = Object.keys(EXTENSION_TYPES).map((e) => `.${e}`).join(',');
  picker.setAttribute('aria-hidden', 'true');
  picker.tabIndex = -1;

  const attach = document.createElement('button');
  attach.type = 'button';
  attach.className = 'icon-btn attach';
  attach.setAttribute('aria-label', 'Attach files');
  attach.title = 'Attach files';
  attach.appendChild(svg('M21 11.5l-8.6 8.6a5 5 0 01-7-7l9-9a3.5 3.5 0 015 5l-9 9a2 2 0 01-2.8-2.8l8.3-8.3'));
  attach.addEventListener('click', () => picker.click());

  const textarea = document.createElement('textarea');
  textarea.className = 'composer-input';
  textarea.rows = 1;
  textarea.placeholder = 'Message the agents…';
  textarea.setAttribute('aria-label', 'Message');
  textarea.maxLength = MAX_TEXT;

  const send = document.createElement('button');
  send.type = 'submit';
  send.className = 'send';
  send.setAttribute('aria-label', 'Send message');
  send.title = 'Send (Enter)';
  send.appendChild(svg('M12 19V5M5 12l7-7 7 7'));

  const stop = document.createElement('button');
  stop.type = 'button';
  stop.className = 'send stop';
  stop.setAttribute('aria-label', 'Stop the run');
  stop.title = 'Stop';
  stop.hidden = true;
  stop.appendChild(svg('M7 7h10v10H7z'));
  stop.addEventListener('click', async () => {
    if (stopping) return;
    stopping = true;
    stop.classList.add('busy');
    stop.title = 'Stopping…';
    try { await onStop(); } finally { /* the stream's done event resets us */ }
  });

  row.append(attach, textarea, send, stop);
  const hint = document.createElement('p');
  hint.className = 'composer-hint';
  hint.textContent = 'Enter to send, Shift+Enter for a new line. The agents can make mistakes; check what matters.';
  form.append(chips, row, picker, hint);
  root.appendChild(form);

  function grow() {
    textarea.style.height = 'auto';
    const max = Math.round(window.innerHeight * 0.35);
    textarea.style.height = `${Math.min(textarea.scrollHeight, max)}px`;
    textarea.style.overflowY = textarea.scrollHeight > max ? 'auto' : 'hidden';
  }
  textarea.addEventListener('input', () => { grow(); refresh(); });
  textarea.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      form.requestSubmit();
    }
  });
  textarea.addEventListener('paste', (e) => {
    const items = Array.from(e.clipboardData?.files || []).filter((f) => f.type.startsWith('image/'));
    if (items.length) {
      e.preventDefault();
      addFiles(items.map((f, i) => (f.name && f.name !== 'image.png' ? f
        : new File([f], `pasted-${Date.now()}-${i + 1}.${f.type === 'image/jpeg' ? 'jpg' : 'png'}`, { type: f.type }))));
    }
  });
  picker.addEventListener('change', () => { addFiles(Array.from(picker.files || [])); picker.value = ''; });

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    if (running || disabled) return;
    const text = textarea.value.trim();
    if (!text && !files.length) return;
    if (!text) { notify('Write a message to go with the files.'); textarea.focus(); return; }
    const sending = files;
    files = [];
    textarea.value = '';
    grow();
    renderChips();
    refresh();
    onSend(text, sending);
  });

  // drag and drop over the whole pane
  const overlay = document.createElement('div');
  overlay.className = 'drop-overlay';
  overlay.hidden = true;
  const overlayText = document.createElement('div');
  overlayText.className = 'drop-text';
  overlayText.textContent = 'Drop files to attach';
  overlay.appendChild(overlayText);
  dropZone.appendChild(overlay);
  let dragDepth = 0;
  const hasFiles = (e) => Array.from(e.dataTransfer?.types || []).includes('Files');
  dropZone.addEventListener('dragenter', (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    dragDepth++;
    overlay.hidden = false;
  });
  dropZone.addEventListener('dragover', (e) => { if (hasFiles(e)) { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; } });
  dropZone.addEventListener('dragleave', (e) => {
    if (!hasFiles(e)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) overlay.hidden = true;
  });
  dropZone.addEventListener('drop', (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    dragDepth = 0;
    overlay.hidden = true;
    addFiles(Array.from(e.dataTransfer.files || []));
    textarea.focus();
  });

  function addFiles(incoming) {
    if (!incoming.length) return;
    const limits = getLimits();
    const { accepted, rejected } = checkAttachments(files, incoming, limits);
    files = files.concat(accepted);
    if (rejected.length) notify(describeRejections(rejected, limits));
    renderChips();
    refresh();
  }

  function removeFile(file) {
    files = files.filter((f) => f !== file);
    const url = urls.get(file);
    if (url) { URL.revokeObjectURL(url); urls.delete(file); }
    renderChips();
    refresh();
    textarea.focus();
  }

  function renderChips() {
    chips.replaceChildren();
    chips.hidden = !files.length;
    for (const f of files) {
      const chip = document.createElement('div');
      chip.className = `chip kind-${fileKind(f.type || '', f.name)}`;
      if (isImageType(f.type)) {
        let url = urls.get(f);
        if (!url) { url = URL.createObjectURL(f); urls.set(f, url); }
        const img = document.createElement('img');
        img.src = url;
        img.alt = '';
        chip.appendChild(img);
      }
      const name = document.createElement('span');
      name.className = 'chip-name';
      name.textContent = f.name;
      name.title = f.name;
      const size = document.createElement('span');
      size.className = 'chip-size';
      size.textContent = formatSize(f.size);
      const rm = document.createElement('button');
      rm.type = 'button';
      rm.className = 'chip-remove';
      rm.setAttribute('aria-label', `Remove ${f.name}`);
      rm.appendChild(svg('M6 6l12 12M18 6L6 18', 14));
      rm.addEventListener('click', () => removeFile(f));
      chip.append(name, size, rm);
      chips.appendChild(chip);
    }
  }

  function refresh() {
    const canSend = !running && !disabled && (textarea.value.trim().length > 0 || files.length > 0);
    send.disabled = !canSend;
    send.hidden = running;
    stop.hidden = !running;
    attach.disabled = disabled || running;
    textarea.disabled = disabled;
    form.classList.toggle('running', running);
  }

  refresh();
  return {
    setRunning(v) {
      running = !!v;
      if (!running) { stopping = false; stop.classList.remove('busy'); stop.title = 'Stop'; }
      refresh();
    },
    setDisabled(v) { disabled = !!v; refresh(); },
    setText(text) { textarea.value = text; grow(); refresh(); textarea.focus(); textarea.setSelectionRange(text.length, text.length); },
    focus() { textarea.focus(); },
    clearFiles() { files = []; renderChips(); refresh(); },
    get files() { return files; },
  };
}
