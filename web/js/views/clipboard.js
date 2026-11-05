import { api } from '../api.js';
import { copyFromPc, queueClipboard } from '../clip.js';
import { back } from '../router.js';
import { state } from '../store.js';
import {
  debounce, disabledCard, empty, errorToast, fmtAgo, fmtBytes, h, icon, pullToRefresh, sheet, skeletonList, swap, swipeRow, toast,
} from '../ui.js';
import { conn } from '../ws.js';

export default function clipboard(root) {
  const wrap = h('div', { class: 'wrap' });
  root.append(wrap);
  const header = h('div', { class: 'section-title' },
    h('button', { class: 'icon-btn back', 'aria-label': 'Back', onclick: () => back('/more') }, icon('chevron-left')),
    h('div', { class: 'h2', style: { flex: 1 } }, 'Clipboard'));
  if (state.features.clipboard === false) {
    wrap.append(header, disabledCard('clipboard', 'Clipboard sync'));
    return {};
  }

  const ta = h('textarea', { class: 'textarea', placeholder: 'Type or paste here, then send it to the PC clipboard', rows: 3 });
  const sendBtn = h('button', { class: 'btn primary', onclick: sendText }, icon('send'), 'Send to PC');
  const imgPick = h('input', { type: 'file', accept: 'image/*', hidden: true });
  const imgBtn = h('button', { class: 'btn square', 'aria-label': 'Send an image', onclick: () => imgPick.click() }, icon('image'));
  const pasteBtn = h('button', { class: 'btn square', 'aria-label': 'Paste from this phone', onclick: pasteFromPhone }, icon('clipboard'));
  const composer = h('div', { class: 'card stack' }, ta, h('div', { class: 'row' }, pasteBtn, imgBtn, h('div', { class: 'spacer' }), sendBtn), imgPick);

  const search = h('input', { class: 'input', type: 'search', placeholder: 'Search history', 'aria-label': 'Search clipboard history' });
  const list = h('div');
  wrap.append(header, composer,
    h('div', { class: 'section-title' }, h('div', { class: 'eyebrow' }, 'History'),
      h('button', { class: 'btn small ghost', onclick: clearAll }, icon('trash'), 'Clear')),
    h('div', { class: 'search', style: { marginBottom: '12px' } }, icon('search'), search),
    list);

  let items = [];
  const revealed = new Map();

  async function sendText() {
    const text = ta.value;
    if (!text) { ta.focus(); return; }
    if (!conn.online) {
      queueClipboard(text);
      ta.value = '';
      toast('Queued', { text: 'It’ll be sent when the PC is back.', kind: 'warn' });
      return;
    }
    sendBtn.disabled = true;
    try {
      await api.post('/clipboard', { text });
      ta.value = '';
      toast('On the PC clipboard', { kind: 'ok', timeout: 1600 });
    } catch (err) {
      if (err.status === 0) { queueClipboard(text); ta.value = ''; toast('Queued for when the PC is back', { kind: 'warn' }); } else errorToast(err);
    } finally {
      sendBtn.disabled = false;
    }
  }

  async function pasteFromPhone() {
    try {
      const text = await navigator.clipboard.readText();
      if (text) { ta.value = text; ta.focus(); }
    } catch {
      toast('Long-press the box and choose Paste', { kind: 'info' });
      ta.focus();
    }
  }

  imgPick.addEventListener('change', async () => {
    const file = imgPick.files[0];
    imgPick.value = '';
    if (!file) return;
    try {
      toast('Sending image…', { ico: 'image', timeout: 1500 });
      await api.post('/clipboard/image', file);
      toast('Image is on the PC clipboard', { kind: 'ok' });
    } catch (err) { errorToast(err); }
  });

  function matches(item, q) {
    if (!q) return true;
    if (item.kind !== 'text') return false;
    return (revealed.get(item.id) || item.preview || '').toLowerCase().includes(q);
  }

  function row(item) {
    const isImg = item.kind === 'image';
    const text = revealed.get(item.id);
    const preview = isImg ? 'Image' : item.secret && !text ? '•'.repeat(Math.min(18, item.length || 12)) : (text || item.preview);
    const thumb = h('div', { class: 'item-icon' }, isImg ? icon('image') : icon(item.secret ? 'lock' : 'file-text'));
    if (isImg) {
      api.blob(`/clipboard/item/${item.id}/image?thumb=1`).then((b) => swap(thumb, h('img', { src: URL.createObjectURL(b), alt: '' }))).catch(() => {});
    }
    const el = h('button', { class: `item clip${item.pinned ? ' pinned' : ''}`, onclick: () => open(item) },
      thumb,
      h('div', { class: 'item-body' },
        h('div', { class: `clip-text${item.secret && !text ? ' masked' : ''}` }, preview),
        h('div', { class: 'item-sub' }, `${item.source === 'pc' ? 'From PC' : 'From phone'} · ${fmtAgo(item.ts)}${isImg ? ` · ${fmtBytes(item.size)}` : ''}`)),
      h('div', { class: 'item-end' }, item.pinned && icon('pin', 'accent')));
    return swipeRow(el, [
      { label: item.pinned ? 'Unpin' : 'Pin', ico: 'pin', cls: 'alt', run: () => pin(item) },
      { label: 'Delete', ico: 'trash', cls: 'del', run: () => del(item) },
    ]);
  }

  function render() {
    const q = search.value.trim().toLowerCase();
    const shown = items.filter((i) => matches(i, q));
    const pinned = shown.filter((i) => i.pinned);
    const rest = shown.filter((i) => !i.pinned);
    if (!shown.length) {
      swap(list, h('div', { class: 'card' }, empty({ ico: 'clipboard', text: q ? 'No matches.' : 'Nothing yet. Copy something on the PC and it shows up here.' })));
      return;
    }
    swap(list,
      pinned.length && h('div', { class: 'list stagger', style: { marginBottom: '12px' } }, pinned.map(row)),
      rest.length && h('div', { class: 'list stagger' }, rest.map(row)));
  }

  async function reveal(item) {
    if (revealed.has(item.id)) return revealed.get(item.id);
    const { text } = await api.get(`/clipboard/item/${item.id}`);
    revealed.set(item.id, text);
    return text;
  }

  function open(item) {
    if (item.kind === 'image') {
      const img = h('img', { class: 'preview-img', alt: 'Clipboard image' });
      api.blob(`/clipboard/item/${item.id}/image`).then((b) => { img.src = URL.createObjectURL(b); }).catch(() => {});
      const s = sheet({ title: 'Image', body: h('div', { class: 'stack' }, img, h('p', { class: 'small faint' }, 'Press and hold the image to save or copy it.')),
        foot: h('div', { class: 'grid-2' },
          h('button', { class: 'btn', onclick: () => apply(item) }, icon('monitor'), 'Copy on PC again'),
          h('button', { class: 'btn danger', onclick: () => { s.close(); del(item); } }, icon('trash'), 'Delete')) });
      return;
    }
    const pre = h('pre', { class: 'clip-full' }, item.secret ? '•'.repeat(16) : (item.preview || ''));
    const revealBtn = item.secret && h('button', { class: 'btn', onclick: async () => {
      try { pre.textContent = await reveal(item); revealBtn.remove(); render(); } catch (err) { errorToast(err); }
    } }, icon('eye'), 'Reveal');
    if (!item.secret && item.length > (item.preview || '').length) reveal(item).then((t) => { pre.textContent = t; }).catch(() => {});
    const s = sheet({ title: item.secret ? 'Hidden clip' : 'Clip', body: h('div', { class: 'stack' },
      item.secret && h('div', { class: 'notice warn small' }, icon('shield'), 'This looks like a password or key, so it’s masked.'),
      pre),
    foot: h('div', { class: 'stack' },
      h('div', { class: 'grid-2' },
        h('button', { class: 'btn primary', onclick: () => copyFromPc(item.id) }, icon('copy'), 'Copy to phone'),
        h('button', { class: 'btn', onclick: () => apply(item) }, icon('monitor'), 'Put on PC')),
      h('div', { class: 'grid-2' },
        revealBtn || h('button', { class: 'btn', onclick: () => pin(item) }, icon('pin'), item.pinned ? 'Unpin' : 'Pin'),
        h('button', { class: 'btn danger', onclick: () => { s.close(); del(item); } }, icon('trash'), 'Delete'))) });
  }

  async function apply(item) {
    try {
      await api.post(`/clipboard/item/${item.id}/apply`);
      toast('Back on the PC clipboard', { kind: 'ok', timeout: 1500 });
    } catch (err) { errorToast(err); }
  }

  async function pin(item) {
    const next = !item.pinned;
    item.pinned = next;
    render();
    try { await api.post(`/clipboard/item/${item.id}/pin`, { pinned: next }); } catch (err) { item.pinned = !next; render(); errorToast(err); }
  }

  async function del(item) {
    const idx = items.indexOf(item);
    items = items.filter((i) => i !== item);
    render();
    try { await api.del(`/clipboard/item/${item.id}`); } catch (err) { items.splice(idx, 0, item); render(); errorToast(err); }
  }

  async function clearAll() {
    const keep = items.filter((i) => i.pinned);
    const old = items;
    items = keep;
    render();
    try {
      await api.del('/clipboard');
      toast('History cleared', { text: keep.length ? 'Pinned clips were kept.' : '', kind: 'ok' });
    } catch (err) { items = old; render(); errorToast(err); }
  }

  async function load() {
    if (!items.length) swap(list, skeletonList(5));
    try {
      ({ items } = await api.get('/clipboard'));
      render();
    } catch (err) {
      swap(list, h('div', { class: 'card' }, empty({ ico: 'wifi-off', text: err.detail || 'Couldn’t load the clipboard.' })));
    }
  }

  search.addEventListener('input', debounce(render, 120));
  const off = conn.on('clipboard', ({ item }) => {
    if (!item) return;
    items = [item, ...items.filter((i) => i.id !== item.id)];
    render();
  });
  pullToRefresh(root, load);
  load();
  return { refresh: load, destroy: off };
}
