import { api } from '../api.js';
import { state } from '../store.js';
import { disabledCard, errorToast, fmtClock, h, icon, sheet, skeletonBlock, slider, swap, tap, throttle } from '../ui.js';
import { conn } from '../ws.js';

export default function media(root) {
  if (state.features.media === false) {
    root.append(disabledCard('media', 'Media and volume'));
    return {};
  }
  const nowCard = h('div', { class: 'card now-playing', hidden: true });
  const volCard = h('div', { class: 'card' });
  const mixer = h('div', { class: 'list' });
  const mixerWrap = h('div', null, h('div', { class: 'section-title' }, h('div', { class: 'eyebrow' }, 'Apps')), mixer);
  const skeleton = h('div', { class: 'stack' }, skeletonBlock(150), skeletonBlock(110), skeletonBlock(200));
  const content = h('div', { class: 'stack', hidden: true }, nowCard, volCard, mixerWrap);
  root.append(skeleton, content);

  let snap = null;
  let pending = 0; // ignore pushes for a moment after the user moves a slider
  let posTimer = null;

  const key = async (name, btn) => {
    if (btn) tap(btn);
    try { await api.post('/media/key', { key: name }); } catch (err) { errorToast(err); }
  };

  // --- master volume ----------------------------------------------------------------

  const volValue = h('div', { class: 'vol-value num' }, '–');
  const muteBtn = h('button', { class: 'btn square', 'aria-label': 'Mute' }, icon('volume'));
  const setVol = throttle((v) => api.post('/media/volume', { value: v }).catch(errorToast), 120);
  const vol = slider({ min: 0, max: 100, step: 1, value: 0, big: true, label: 'Master volume',
    onInput: (v) => { pending = Date.now(); volValue.textContent = `${v}`; paintMute(false, v); setVol(v); } });
  const devBtn = h('button', { class: 'btn small ghost', onclick: chooseDevice }, icon('headphones'), h('span', { class: 'ellipsis dev-name' }, 'Output'));
  swap(volCard,
    h('div', { class: 'card-head' }, icon('volume'), h('div', { class: 'h3' }, 'Volume'), devBtn),
    h('div', { class: 'row' }, muteBtn, h('div', { style: { flex: 1 } }, vol), volValue));

  const paintMute = (muted, v) => {
    swap(muteBtn, icon(muted || v === 0 ? 'volume-x' : v < 50 ? 'volume-1' : 'volume'));
    muteBtn.classList.toggle('danger', !!muted);
    muteBtn.setAttribute('aria-pressed', String(!!muted));
  };
  muteBtn.addEventListener('click', async () => {
    const next = !snap?.muted;
    paintMute(next, snap?.volume);
    if (snap) snap.muted = next;
    try { await api.post('/media/mute', { muted: next }); } catch (err) { paintMute(!next, snap?.volume); errorToast(err); }
  });

  function chooseDevice() {
    const devs = snap?.devices || [];
    if (!devs.length) return;
    const s = sheet({ title: 'Output device', body: h('div', { class: 'list' }, devs.map((d) => h('button', {
      class: 'item', onclick: async () => {
        try {
          const res = await api.post('/media/device', { id: d.id });
          snap.devices = res.devices;
          render();
          s.close();
        } catch (err) { errorToast(err); }
      },
    }, h('div', { class: `item-icon ${d.default ? 'accent' : ''}` }, icon('headphones')),
    h('div', { class: 'item-body' }, h('div', { class: 'item-title' }, d.name)),
    d.default && h('div', { class: 'item-end accent' }, icon('check'))))) });
  }

  // --- now playing -------------------------------------------------------------------

  function renderNow(n) {
    nowCard.hidden = !snap?.can?.now_playing;
    if (nowCard.hidden) return;
    clearInterval(posTimer);
    const playing = n?.status === 'playing';
    const art = n?.art ? h('img', { class: 'np-art', src: n.art, alt: '' }) : h('div', { class: 'np-art empty-art' }, icon('music'));
    const pos = h('span', { class: 'num' }, fmtClock(n?.position));
    const bar = h('i');
    const paintPos = (p) => {
      pos.textContent = fmtClock(p);
      bar.style.width = n?.duration ? `${Math.min(100, (p / n.duration) * 100)}%` : '0%';
    };
    swap(nowCard,
      h('div', { class: 'np' }, art,
        h('div', { class: 'np-meta' },
          h('div', { class: 'eyebrow' }, n ? (n.app || 'Now playing') : 'Nothing playing'),
          h('div', { class: 'np-title ellipsis' }, n?.title || 'No media session'),
          h('div', { class: 'np-artist ellipsis dim' }, n?.artist || 'Start something on the PC'))),
      n?.duration ? h('div', { class: 'np-progress' }, h('div', { class: 'bar thin' }, bar),
        h('div', { class: 'row tiny faint num', style: { justifyContent: 'space-between' } }, pos, h('span', null, fmtClock(n.duration)))) : null,
      h('div', { class: 'transport' },
        h('button', { class: 'tr-btn', 'aria-label': 'Previous', onclick: (e) => key('prev', e.currentTarget) }, icon('prev')),
        h('button', { class: 'tr-btn main', 'aria-label': playing ? 'Pause' : 'Play', onclick: (e) => key('play_pause', e.currentTarget) }, icon(playing ? 'pause' : 'play')),
        h('button', { class: 'tr-btn', 'aria-label': 'Next', onclick: (e) => key('next', e.currentTarget) }, icon('next'))));
    if (n?.duration && n.position != null) {
      paintPos(n.position);
      if (playing) {
        const t0 = Date.now();
        posTimer = setInterval(() => paintPos(Math.min(n.duration, n.position + (Date.now() - t0) / 1000)), 1000);
      }
    }
  }

  // --- mixer ---------------------------------------------------------------------------

  const mixRows = new Map();

  function renderMixer(sessions) {
    mixerWrap.hidden = !sessions?.length;
    const seen = new Set();
    for (const s of sessions || []) {
      seen.add(s.id);
      let row = mixRows.get(s.id);
      if (!row) {
        const label = h('span', { class: 'num tiny faint mix-val' });
        const mute = h('button', { class: 'icon-btn', 'aria-label': `Mute ${s.name}` });
        const send = throttle((v) => api.post('/media/session', { id: s.id, volume: v }).catch(errorToast), 120);
        const sl = slider({ min: 0, max: 100, value: s.volume, label: `${s.name} volume`,
          onInput: (v) => { pending = Date.now(); label.textContent = v; send(v); } });
        mute.addEventListener('click', async () => {
          const cur = row.data.muted;
          row.data.muted = !cur;
          paintRow(row);
          try { await api.post('/media/session', { id: s.id, muted: !cur }); } catch (err) { row.data.muted = cur; paintRow(row); errorToast(err); }
        });
        const ic = s.icon ? h('img', { src: s.icon, alt: '' }) : icon(s.id === 'system' ? 'bell' : 'music');
        const el = h('div', { class: 'item mix' },
          h('div', { class: 'item-icon' }, ic),
          h('div', { class: 'item-body' },
            h('div', { class: 'row', style: { gap: '6px' } }, h('div', { class: 'item-title', style: { flex: 1 } }, s.name), label),
            sl),
          mute);
        row = { el, sl, label, mute, data: s };
        mixRows.set(s.id, row);
        mixer.append(el);
      }
      row.data = { ...row.data, ...s };
      paintRow(row);
    }
    for (const [id, row] of mixRows) if (!seen.has(id)) { row.el.remove(); mixRows.delete(id); }
  }

  function paintRow(row) {
    if (Date.now() - pending > 1500) row.sl.set(row.data.volume);
    row.label.textContent = row.data.volume;
    swap(row.mute, icon(row.data.muted ? 'volume-x' : 'volume-1'));
    row.mute.classList.toggle('danger', !!row.data.muted);
    row.el.classList.toggle('muted', !!row.data.muted);
  }

  function render() {
    if (!snap) return;
    skeleton.hidden = true;
    content.hidden = false;
    if (Date.now() - pending > 1500) {
      vol.set(snap.volume);
      volValue.textContent = `${snap.volume}`;
    }
    paintMute(snap.muted, snap.volume);
    const def = snap.devices?.find((d) => d.default);
    devBtn.hidden = !(snap.devices?.length > 1);
    devBtn.querySelector('.dev-name').textContent = def?.name || 'Output';
    renderNow(snap.now);
    if (snap.sessions) renderMixer(snap.sessions);
  }

  const off = conn.on('media', (data) => {
    snap = { ...snap, ...data, sessions: data.sessions ?? snap?.sessions, devices: data.devices ?? snap?.devices };
    render();
  });
  const unsub = conn.sub('media');
  const load = () => api.get('/media').then((d) => { snap = d; render(); }).catch((err) => {
    if (!snap) swap(skeleton, h('div', { class: 'card' }, h('div', { class: 'notice danger' }, icon('alert'), err.detail || 'Audio controls unavailable.')));
  });
  load();
  return { refresh: load, destroy() { off(); unsub(); clearInterval(posTimer); } };
}
