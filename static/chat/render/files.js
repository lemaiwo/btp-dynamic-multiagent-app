// File cards: icon by type, name, size, Download. Uploaded PNG/JPEG get a
// thumbnail fetched as a blob (the file route needs the X-Requested-With
// header, which an <img src> cannot send).

import { fileUrl, fetchFileBlob } from '../api.js';
import { formatSize, fileKind, isImageType } from '../util.js';

const ICONS = {
  image: 'M4 5h16v14H4zM6 7v8l4-4 3 3 2-2 3 3V7z',
  pdf: 'M6 2h8l4 4v16H6zM14 2v4h4M8 13h8M8 17h8',
  sheet: 'M4 4h16v16H4zM4 10h16M4 15h16M10 4v16',
  doc: 'M6 2h8l4 4v16H6zM14 2v4h4M8 12h8M8 16h8M8 8h3',
  code: 'M8 9l-4 3 4 3M16 9l4 3-4 3M13 6l-2 12',
  text: 'M5 4h14M5 9h14M5 14h10M5 19h8',
  file: 'M6 2h8l4 4v16H6zM14 2v4h4',
};

function icon(kind) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  p.setAttribute('d', ICONS[kind] || ICONS.file);
  svg.appendChild(p);
  return svg;
}

const thumbs = new Map(); // file id -> object URL promise

function thumbnail(id) {
  if (!thumbs.has(id)) {
    thumbs.set(id, fetchFileBlob(id).then((b) => URL.createObjectURL(b)).catch(() => null));
  }
  return thumbs.get(id);
}

/** One card. `file` = {id|file_id, name, media_type, size}. */
export function fileCard(file) {
  const id = file.id || file.file_id;
  const kind = fileKind(file.media_type, file.name);
  const card = document.createElement('div');
  card.className = `file-card kind-${kind}`;
  const tile = document.createElement('div');
  tile.className = 'file-tile';
  tile.appendChild(icon(kind));
  if (id && isImageType(file.media_type)) {
    thumbnail(id).then((url) => {
      if (!url) return;
      const img = document.createElement('img');
      img.src = url;
      img.alt = '';
      tile.replaceChildren(img);
      card.classList.add('has-thumb');
    });
  }
  const meta = document.createElement('div');
  meta.className = 'file-meta';
  const name = document.createElement('div');
  name.className = 'file-name';
  name.textContent = file.name || 'file';
  name.title = file.name || '';
  const sub = document.createElement('div');
  sub.className = 'file-sub';
  sub.textContent = formatSize(file.size);
  meta.append(name, sub);
  card.append(tile, meta);
  if (id) {
    const a = document.createElement('a');
    a.className = 'file-download';
    a.href = fileUrl(id);
    a.setAttribute('download', file.name || '');
    a.textContent = 'Download';
    a.setAttribute('aria-label', `Download ${file.name || 'file'}`);
    card.appendChild(a);
  }
  return card;
}

export function fileCards(files) {
  const row = document.createElement('div');
  row.className = 'file-row';
  for (const f of files) row.appendChild(fileCard(f));
  return row;
}
