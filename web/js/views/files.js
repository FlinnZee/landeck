import { api, url } from '../api.js';
import { openPlayer } from '../player.js';
import { navigate } from '../router.js';
import { prefs, setPref, state } from '../store.js';
import {
  debounce, disabledCard, empty, errorToast, fmtBytes, fmtDate, fmtDuration, fmtRate, h, holdButton, icon, kindIcon,
  pullToRefresh, sheet, skeletonList, swap, swipeRow, toast, toggle,
} from '../ui.js';
import { conn } from '../ws.js';

const enc = (p) => p.split('/').map(encodeURIComponent).join('/');
const dec = (p) => (p || '').split('/').filter(Boolean).map(decodeURIComponent).join('/');

// --- uploads (module level, so they survive navigating away) --------------------------

const uploads = [];
const uploadListeners = new Set();
let pumping = false;

const notifyUploads = () => uploadListeners.forEach((fn) => fn());

export function enqueueUploads(files, target) {
  for (const file of files) {
    uploads.push({ file, name: file.name, size: file.size, target, id: null, received: 0, status: 'queued', error: '', samples: [] });
  }
  notifyUploads();
  pump();
}

async function pump() {
  if (pumping) return;
  pumping = true;
  try {
    for (;;) {
      const job = uploads.find((u) => u.status === 'queued' || u.status === 'retry');
      if (!job) break;
      await runUpload(job);
    }
  } finally {
    pumping = false;
  }
}

function putChunk(job, offset, blob) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    job.xhr = xhr;
    xhr.open('PUT', url(`/files/upload/${job.id}?offset=${offset}`));
    xhr.setRequestHeader('Authorization', `Bearer ${conn.token}`);
    xhr.setRequestHeader('X-LanDeck', '1');
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.upload.onprogress = (e) => {
      job.received = offset + e.loaded;
      job.samples.push([performance.now(), job.received]);
      if (job.samples.length > 40) job.samples.shift();
      notifyUploads();
    };
    xhr.onload = () => {
      let body = {};
      try { body = JSON.parse(xhr.responseText); } catch { /* */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body);
      else reject(Object.assign(new Error(body.detail || `HTTP ${xhr.status}`), { status: xhr.status, error: body.error, received: body.received }));
    };
    xhr.onerror = () => reject(Object.assign(new Error('network'), { status: 0 }));
    xhr.onabort = () => reject(Object.assign(new Error('aborted'), { status: -1 }));
    xhr.send(blob);
  });
}

async function runUpload(job) {
  job.status = 'uploading';
  notifyUploads();
  try {
    if (!job.id) {
      const res = await api.post('/files/upload', { name: job.name, size: job.size, root: job.target?.root, path: job.target?.path });
      job.id = res.id;
      job.chunk = res.chunk_size;
    } else {
      // resuming: ask the PC how much it already has
      const res = await api.get(`/files/upload/${job.id}`);
      job.received = res.received;
      job.chunk = res.chunk_size;
    }
    while (job.received < job.size) {
      if (job.status === 'cancelled') return;
      const offset = job.received;
      const blob = job.file.slice(offset, Math.min(job.size, offset + job.chunk));
      try {
        const res = await putChunk(job, offset, blob);
        job.received = res.received;
      } catch (err) {
        if (err.status === 409 && err.received != null) { job.received = err.received; continue; }
        throw err;
      }
    }
    const done = await api.post(`/files/upload/${job.id}/complete`);
    job.status = 'done';
    job.result = done;
    notifyUploads();
  } catch (err) {
    if (job.status === 'cancelled') return;
    if (err.status === 0 || err.status === -1 || err.status === 503) {
      // network gone: wait for the connection manager, then resume from the server's offset
      job.status = 'paused';
      notifyUploads();
      await new Promise((resolve) => {
        const off = conn.on('online', () => { off(); resolve(); });
      });
      if (job.status === 'paused') job.status = 'retry';
      notifyUploads();
      return;
    }
    job.status = 'error';
    job.error = err.detail || err.message;
    notifyUploads();
  }
}

function cancelUpload(job) {
  job.status = 'cancelled';
  job.xhr?.abort();
  if (job.id) api.del(`/files/upload/${job.id}`).catch(() => {});
  uploads.splice(uploads.indexOf(job), 1);
  notifyUploads();
}

function speedOf(job) {
  const s = job.samples;
  if (s.length < 2) return 0;
  const [t0, b0] = s[0];
  const [t1, b1] = s[s.length - 1];
  return t1 > t0 ? ((b1 - b0) / (t1 - t0)) * 1000 : 0;
}

function uploadPanel() {
  const el = h('div', { class: 'card upload-panel', hidden: true });
  const render = () => {
    const active = uploads.filter((u) => u.status !== 'cancelled');
    el.hidden = !active.length;
    if (!active.length) return;
    const pending = active.filter((u) => u.status !== 'done');
    const total = pending.reduce((a, u) => a + u.size, 0);
    const got = pending.reduce((a, u) => a + u.received, 0);
    const speed = pending.reduce((a, u) => a + (u.status === 'uploading' ? speedOf(u) : 0), 0);
    const eta = speed > 0 ? (total - got) / speed : null;
    const head = pending.length
      ? `${pending.length} uploading · ${fmtRate(speed)}${eta ? ` · ${fmtDuration(eta)} left` : ''}`
      : 'Uploads finished';
    swap(el,
      h('div', { class: 'card-head' }, icon('upload'), h('div', { class: 'h3 num' }, head),
        !pending.length && h('button', { class: 'btn small ghost', onclick: () => { for (const u of [...uploads]) if (u.status === 'done' || u.status === 'error') uploads.splice(uploads.indexOf(u), 1); notifyUploads(); } }, 'Clear')),
      h('div', { class: 'stack', style: { gap: '10px' } }, active.slice(-6).map((u) => {
        const pct = u.size ? (u.received / u.size) * 100 : 100;
        const label = { queued: 'Waiting', uploading: `${Math.floor(pct)}%`, paused: 'Paused – reconnecting', retry: 'Resuming', done: 'Done', error: u.error || 'Failed' }[u.status];
        return h('div', { class: 'up-row' },
          h('div', { class: 'row', style: { gap: '8px' } },
            h('div', { class: 'ellipsis small', style: { flex: 1 } }, u.name),
            h('span', { class: `tiny num ${u.status === 'error' ? 'danger' : u.status === 'done' ? 'ok' : 'faint'}` }, label),
            u.status !== 'done' && h('button', { class: 'icon-btn tiny-btn', 'aria-label': `Cancel ${u.name}`, onclick: () => cancelUpload(u) }, icon('x'))),
          h('div', { class: `bar ${u.status === 'error' ? 'danger' : ''}` }, h('i', { style: { width: `${pct}%` } })));
      })));
  };
  uploadListeners.add(render);
  render();
  return { el, destroy: () => uploadListeners.delete(render) };
}

// --- view ---------------------------------------------------------------------------

const SORTS = {
  name: (a, b) => (b.dir - a.dir) || a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' }),
  date: (a, b) => (b.dir - a.dir) || b.mtime - a.mtime,
  size: (a, b) => (b.dir - a.dir) || b.size - a.size,
};

export default function files(root, { match }) {
  if (state.features.files === false) {
    root.append(h('div', { class: 'wrap' }, disabledCard('files', 'File access')));
    return {};
  }
  const wrap = h('div', { class: 'wrap files' });
  const head = h('div', { class: 'files-head' });
  const body = h('div', { class: 'files-body' });
  const panel = uploadPanel();
  const picker = h('input', { type: 'file', multiple: true, hidden: true });
  const fab = h('button', { class: 'fab needs-online', 'aria-label': 'Upload files', onclick: () => picker.click() }, icon('upload'));
  wrap.append(head, body, panel.el, picker);
  root.append(wrap, fab);

  let loc = { root: null, path: '' };
  let listing = null;
  let roots = [];
  let searchCtrl = null;
  let searching = false;

  picker.addEventListener('change', () => {
    if (!picker.files.length) return;
    enqueueUploads([...picker.files], loc.root ? { root: loc.root, path: loc.path } : null);
    toast(`Uploading ${picker.files.length} file${picker.files.length > 1 ? 's' : ''}`, {
      text: loc.root ? `to ${currentName()}` : 'to Downloads\\LanDeck on the PC', ico: 'upload',
    });
    picker.value = '';
  });

  // drag and drop (desktop browsers)
  root.addEventListener('dragover', (e) => { e.preventDefault(); root.classList.add('dropping'); });
  root.addEventListener('dragleave', () => root.classList.remove('dropping'));
  root.addEventListener('drop', (e) => {
    e.preventDefault();
    root.classList.remove('dropping');
    if (e.dataTransfer.files.length) enqueueUploads([...e.dataTransfer.files], loc.root ? { root: loc.root, path: loc.path } : null);
  });

  const offDone = (() => {
    const fn = () => {
      const fresh = uploads.find((u) => u.status === 'done' && !u.seen);
      if (fresh) {
        uploads.filter((u) => u.status === 'done').forEach((u) => { u.seen = true; });
        if (loc.root && fresh.target?.root === loc.root && (fresh.target?.path || '') === loc.path) load(true);
      }
    };
    uploadListeners.add(fn);
    return () => uploadListeners.delete(fn);
  })();

  const currentName = () => (loc.path ? loc.path.split('/').pop() : roots.find((r) => r.id === loc.root)?.name || 'Folder');

  function parseLoc(m) {
    const rest = (m && m[1]) || '';
    const [rootId, ...parts] = rest.split('/').filter(Boolean);
    return { root: rootId ? decodeURIComponent(rootId) : null, path: dec(parts.join('/')) };
  }

  function go(rootId, path = '') {
    navigate(`/files/${encodeURIComponent(rootId)}${path ? `/${enc(path)}` : ''}`);
  }

  // --- header ----------------------------------------------------------------------

  function renderHead() {
    if (!loc.root) {
      swap(head, h('div', { class: 'section-title' }, h('div', { class: 'h2' }, 'Files'),
        h('button', { class: 'btn small ghost needs-online', onclick: () => picker.click() }, icon('upload'), 'Upload')));
      return;
    }
    const rootName = roots.find((r) => r.id === loc.root)?.name || loc.root;
    const parts = loc.path ? loc.path.split('/') : [];
    const crumbs = h('nav', { class: 'crumbs', 'aria-label': 'Folder path' },
      h('button', { class: 'crumb', onclick: () => navigate('/files') }, icon('home')),
      h('button', { class: 'crumb', onclick: () => go(loc.root) }, rootName),
      parts.map((p, i) => h('button', { class: 'crumb', onclick: () => go(loc.root, parts.slice(0, i + 1).join('/')) }, p)));
    requestAnimationFrame(() => { crumbs.scrollLeft = crumbs.scrollWidth; });

    const search = h('input', { class: 'input', type: 'search', placeholder: `Search in ${currentName()}`, enterkeyhint: 'search', 'aria-label': 'Search files' });
    const runSearch = debounce(() => doSearch(search.value.trim()), 380);
    search.addEventListener('input', runSearch);

    const sortChips = h('div', { class: 'chips' },
      ['name', 'date', 'size'].map((s) => h('button', { class: 'chip', 'aria-pressed': String(prefs.filesSort === s), onclick: () => { setPref('filesSort', s); renderHead(); renderList(); } },
        { name: 'Name', date: 'Recent', size: 'Size' }[s])),
      h('button', { class: 'chip', onclick: newFolder }, icon('folder-plus'), 'New folder'),
      h('button', { class: 'chip', onclick: moreSheet }, icon('more')));

    swap(head, crumbs, h('div', { class: 'search' }, icon('search'), search), sortChips);
  }

  // --- roots -----------------------------------------------------------------------

  async function loadRoots() {
    try {
      const res = await api.get('/files/roots');
      roots = res.roots;
      return res;
    } catch (err) {
      if (!loc.root) swap(body, h('div', { class: 'card' }, empty({ ico: 'wifi-off', text: err.detail || 'Couldn’t load folders.' })));
      return null;
    }
  }

  async function showRoots() {
    renderHead();
    swap(body, skeletonList(5));
    const res = await loadRoots();
    if (!res) return;
    if (!roots.length) {
      swap(body, h('div', { class: 'card' }, empty({ ico: 'folder', text: 'No folders are shared.', sub: 'Add folders in the admin page on the PC.' })));
      return;
    }
    swap(body,
      h('div', { class: 'list stagger' }, roots.map((r) => {
        const used = r.disk ? 1 - r.disk.free / r.disk.total : 0;
        const ic = r.drive ? 'drive'
          : r.id === 'downloads' ? 'download' : r.id === 'pictures' ? 'image' : r.id === 'videos' ? 'film' : 'folder';
        return h('button', { class: 'item', onclick: () => go(r.id) },
          h('div', { class: 'item-icon accent' }, icon(ic)),
          h('div', { class: 'item-body' }, h('div', { class: 'item-title' }, r.name),
            h('div', { class: 'item-sub' }, r.disk ? `${fmtBytes(r.disk.free)} free of ${fmtBytes(r.disk.total)}` : r.path),
            r.disk && h('div', { class: `bar thin ${used > 0.9 ? 'danger' : ''}`, style: { marginTop: '6px' } }, h('i', { style: { width: `${used * 100}%` } }))),
          h('div', { class: 'item-end' }, icon('chevron-right')));
      })),
      h('p', { class: 'small faint', style: { margin: '14px 4px' } }, `Uploads from here go to ${res.upload_dir}.`));
  }

  // --- folder ----------------------------------------------------------------------

  async function load(quiet = false) {
    if (!roots.length) await loadRoots();
    renderHead();
    if (!quiet) swap(body, skeletonList(8));
    try {
      listing = await api.get(`/files/list?root=${encodeURIComponent(loc.root)}&path=${encodeURIComponent(loc.path)}&hidden=${prefs.filesHidden ? 1 : 0}`);
      renderList();
    } catch (err) {
      swap(body, h('div', { class: 'card' }, empty({
        ico: err.status === 404 ? 'folder' : 'alert',
        text: err.detail || 'Couldn’t open this folder.',
        action: { label: 'Back to folders', run: () => navigate('/files') },
      })));
    }
  }

  function row(item, { showPath = false } = {}) {
    const thumb = item.thumb ? h('img', { src: url(item.thumb), alt: '', loading: 'lazy', decoding: 'async', onerror: (e) => e.target.replaceWith(icon(kindIcon(item.kind))) }) : icon(kindIcon(item.kind));
    const sub = item.dir ? fmtDate(item.mtime) : `${fmtBytes(item.size)} · ${fmtDate(item.mtime)}`;
    const el = h('button', { class: 'item', onclick: () => (item.dir ? go(loc.root, item.path) : preview(item)) },
      h('div', { class: `item-icon ${item.dir ? 'accent' : ''} kind-${item.kind}` }, thumb),
      h('div', { class: 'item-body' }, h('div', { class: 'item-title' }, item.name),
        h('div', { class: 'item-sub' }, showPath ? item.path.split('/').slice(0, -1).join('/') || '/' : sub)),
      h('div', { class: 'item-end' }, item.dir ? icon('chevron-right') : null));
    return swipeRow(el, [
      { label: 'Rename', ico: 'pencil', cls: 'alt', run: () => rename(item) },
      { label: 'Delete', ico: 'trash', cls: 'del', run: () => remove([item]) },
    ]);
  }

  function renderList() {
    if (!listing || searching) return;
    const items = [...listing.items].sort(SORTS[prefs.filesSort] || SORTS.name);
    if (!items.length) {
      swap(body, h('div', { class: 'card' }, empty({ ico: 'folder', text: 'This folder is empty.', action: { label: 'Upload here', run: () => picker.click() } })));
      return;
    }
    const dirs = items.filter((i) => i.dir).length;
    swap(body,
      h('div', { class: 'list stagger' }, items.slice(0, 400).map((i) => row(i))),
      items.length > 400 && h('p', { class: 'small faint', style: { margin: '12px 4px' } }, `Showing 400 of ${items.length}. Use search to find the rest.`),
      h('p', { class: 'tiny faint num', style: { margin: '12px 4px' } },
        `${dirs} folder${dirs === 1 ? '' : 's'}, ${items.length - dirs} file${items.length - dirs === 1 ? '' : 's'}`,
        listing.disk ? ` · ${fmtBytes(listing.disk.free)} free` : '',
        listing.truncated ? ' · list truncated' : ''));
  }

  async function doSearch(q) {
    searchCtrl?.abort();
    if (!q) { searching = false; renderList(); return; }
    searching = true;
    searchCtrl = new AbortController();
    const mine = searchCtrl;
    swap(body, h('div', { class: 'row small dim', style: { padding: '12px 4px' } }, h('div', { class: 'spinner' }), `Searching for “${q}”…`));
    try {
      const res = await api.get(`/files/search?root=${encodeURIComponent(loc.root)}&path=${encodeURIComponent(loc.path)}&q=${encodeURIComponent(q)}`, { signal: mine.signal, timeout: 30000 });
      if (mine !== searchCtrl) return;
      swap(body, res.items.length
        ? h('div', { class: 'list stagger' }, res.items.map((i) => row(i, { showPath: true })))
        : h('div', { class: 'card' }, empty({ ico: 'search', text: `Nothing matches “${q}”.` })),
      (res.timed_out || res.truncated) && h('p', { class: 'small faint', style: { margin: '12px 4px' } }, res.timed_out ? 'Stopped after 15 s; narrow the search to look deeper.' : 'Showing the first 300 matches.'));
    } catch (err) {
      if (err.name === 'AbortError') return;
      swap(body, h('div', { class: 'card' }, empty({ ico: 'alert', text: err.detail || 'Search failed.' })));
    }
  }

  // --- actions ----------------------------------------------------------------------

  function preview(item) {
    const src = url(item.url);
    let media;
    if (item.kind === 'image') media = h('img', { class: 'preview-img', src, alt: item.name });
    else if (item.kind === 'video') {
      // stream it (works for MKV/HEVC/HDR, not just MP4) via the full-screen player
      media = h('button', { class: 'preview-play', onclick: () => { s.close(); openPlayer(loc.root, item.path, item.name); } },
        h('div', { class: 'play-circle' }, icon('play')), h('div', { class: 'small dim' }, 'Play on phone'));
    } else if (item.kind === 'audio') media = h('audio', { class: 'preview-audio', src, controls: true, preload: 'metadata' });
    else if (item.kind === 'text' || item.kind === 'code') {
      media = h('pre', { class: 'preview-text' }, 'Loading…');
      fetch(src, { headers: { Range: 'bytes=0-200000' } }).then((r) => r.text()).then((t) => { media.textContent = t || '(empty file)'; })
        .catch(() => { media.textContent = 'Couldn’t load a preview.'; });
    } else {
      media = h('div', { class: 'preview-generic' }, h('div', { class: 'empty-icon' }, icon(kindIcon(item.kind))), h('div', { class: 'dim small' }, item.kind === 'pdf' ? 'PDF document' : 'No preview for this type'));
    }
    const s = sheet({
      title: item.name,
      body: h('div', { class: 'stack' }, media,
        h('div', { class: 'kv small' },
          h('div', { class: 'kv-row' }, h('span', { class: 'faint' }, 'Size'), h('span', { class: 'num' }, fmtBytes(item.size))),
          h('div', { class: 'kv-row' }, h('span', { class: 'faint' }, 'Modified'), h('span', null, new Date(item.mtime * 1000).toLocaleString())),
          h('div', { class: 'kv-row' }, h('span', { class: 'faint' }, 'Folder'), h('span', { class: 'ellipsis' }, `${roots.find((r) => r.id === loc.root)?.name || ''}/${item.path.split('/').slice(0, -1).join('/')}`)))),
      foot: h('div', { class: 'stack' },
        h('div', { class: 'grid-2' },
          h('a', { class: 'btn', href: `${src}?dl=1`, download: item.name }, icon('download'), 'Download'),
          item.kind === 'pdf' ? h('a', { class: 'btn', href: src, target: '_blank', rel: 'noopener' }, icon('external'), 'Open') : h('button', { class: 'btn', onclick: () => share(item) }, icon('share'), 'Share')),
        h('div', { class: 'grid-2' },
          h('button', { class: 'btn', onclick: () => openOnPc(item) }, icon('monitor'), 'Open on PC'),
          h('button', { class: 'btn', onclick: () => reveal(item) }, icon('folder'), 'Show in Explorer')),
        h('div', { class: 'grid-2' },
          h('button', { class: 'btn', onclick: () => { s.close(); rename(item); } }, icon('pencil'), 'Rename'),
          holdButton({ label: 'Delete', onConfirm: () => { s.close(); doDelete([item]); } }))),
      onClose: () => { media.pause?.(); },
    });
  }

  async function share(item) {
    try {
      if (navigator.share) {
        await navigator.share({ title: item.name, url: url(item.url) });
        return;
      }
    } catch { return; }
    toast('Sharing isn’t available here', { kind: 'warn' });
  }

  async function openOnPc(item, confirm = false) {
    try {
      await api.post('/files/open', { root: loc.root, path: item.path, confirm });
      toast('Opened on the PC', { kind: 'ok', text: item.name });
    } catch (err) {
      if (err.error === 'confirm_required') {
        const s = sheet({ title: 'Run this program?', body: h('div', { class: 'stack' },
          h('p', { class: 'dim' }, `${item.name} can run code on the PC with LanDeck’s admin rights. Only continue if you trust it.`),
          holdButton({ label: 'Hold to open', onConfirm: () => { s.close(); openOnPc(item, true); } })) });
      } else errorToast(err);
    }
  }

  async function reveal(item) {
    try {
      await api.post('/files/reveal', { root: loc.root, path: item.path });
      toast('Shown in Explorer on the PC', { kind: 'ok' });
    } catch (err) { errorToast(err); }
  }

  function nameSheet({ title, value = '', placeholder, confirm, run }) {
    const input = h('input', { class: 'input', value, placeholder, autofocus: true, autocapitalize: 'off', autocomplete: 'off' });
    const go2 = async () => {
      const v = input.value.trim();
      if (!v) return;
      btn.disabled = true;
      try {
        await run(v);
        s.close();
      } catch (err) {
        btn.disabled = false;
        errorToast(err);
      }
    };
    const btn = h('button', { class: 'btn primary block', onclick: go2 }, confirm);
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') go2(); });
    const s = sheet({ title, body: h('div', { class: 'stack' }, input, btn) });
    if (value) setTimeout(() => { const dot = value.lastIndexOf('.'); input.setSelectionRange(0, dot > 0 ? dot : value.length); }, 350);
  }

  function rename(item) {
    nameSheet({ title: 'Rename', value: item.name, confirm: 'Rename', run: async (name) => {
      await api.post('/files/rename', { root: loc.root, path: item.path, name });
      toast('Renamed', { kind: 'ok', text: name });
      load(true);
    } });
  }

  function newFolder() {
    nameSheet({ title: 'New folder', placeholder: 'Folder name', confirm: 'Create', run: async (name) => {
      await api.post('/files/mkdir', { root: loc.root, path: loc.path, name });
      load(true);
    } });
  }

  function remove(items) {
    const s = sheet({ title: items.length > 1 ? `Delete ${items.length} items?` : `Delete “${items[0].name}”?`, body: h('div', { class: 'stack' },
      h('p', { class: 'dim' }, 'It goes to the Recycle Bin on the PC, so you can still restore it there.'),
      holdButton({ label: 'Hold to delete', onConfirm: () => { s.close(); doDelete(items); } })) });
  }

  async function doDelete(items) {
    try {
      await api.post('/files/delete', { root: loc.root, paths: items.map((i) => i.path), confirm: true });
      toast('Moved to Recycle Bin', { kind: 'ok', text: items.map((i) => i.name).join(', ') });
      load(true);
    } catch (err) { errorToast(err); }
  }

  function moreSheet() {
    const t = toggle(prefs.filesHidden, (v) => { setPref('filesHidden', v); load(true); }, 'Show hidden files');
    sheet({ title: currentName(), body: h('div', { class: 'stack' },
      h('label', { class: 'row', style: { justifyContent: 'space-between' } }, h('span', null, 'Show hidden files'), t),
      listing?.zip && h('a', { class: 'btn', href: url(listing.zip), download: `${currentName()}.zip` }, icon('archive'), 'Download folder as .zip'),
      h('button', { class: 'btn', onclick: () => picker.click() }, icon('upload'), 'Upload here')) });
  }

  function apply(m) {
    const next = parseLoc(m);
    const changed = next.root !== loc.root || next.path !== loc.path;
    loc = next;
    searching = false;
    searchCtrl?.abort();
    fab.hidden = false;
    if (!loc.root) showRoots();
    else if (changed || !listing) {
      listing = null;
      load();
      root.scrollTop = 0;
    }
  }

  pullToRefresh(root, () => (loc.root ? load(true) : showRoots()));
  apply(match);

  return {
    update({ match: m }) { apply(m); },
    refresh() { if (loc.root) load(true); else showRoots(); },
    destroy() { panel.destroy(); offDone(); searchCtrl?.abort(); },
  };
}
