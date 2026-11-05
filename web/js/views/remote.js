import { api } from '../api.js';
import { navigate } from '../router.js';
import { load, prefs, save, setPref, state } from '../store.js';
import { disabledCard, errorToast, h, icon, segmented, sheet, slider, swap, tap, toast, toggle } from '../ui.js';
import { conn } from '../ws.js';

// --- input batching ------------------------------------------------------------

const outbox = [];
let flushQueued = false;

function queue(ev) {
  outbox.push(ev);
  if (!flushQueued) {
    flushQueued = true;
    requestAnimationFrame(flush);
  }
}

function flush() {
  flushQueued = false;
  if (!outbox.length) return;
  // coalesce consecutive moves and scrolls into one event each
  const batch = [];
  for (const ev of outbox.splice(0)) {
    const prev = batch[batch.length - 1];
    if (prev && (ev[0] === 'm' || ev[0] === 's') && prev[0] === ev[0]) {
      prev[1] += ev[1];
      prev[2] += ev[2];
    } else batch.push([...ev]);
  }
  const msg = { type: 'input', ev: batch.map((e) => (e[0] === 'm' ? ['m', +e[1].toFixed(2), +e[2].toFixed(2)] : e)) };
  if (!conn.send(msg)) {
    // socket not open: fall back to REST for anything that isn't pointer motion
    const important = batch.filter((e) => e[0] !== 'm' && e[0] !== 's');
    if (important.length) api.post('/input', { ev: important }).catch((err) => errorToast(err));
  }
}

export function sendKeys(keys) {
  queue(['c', keys]);
}

export function sendText(text) {
  if (text) queue(['t', text]);
}

// --- trackpad --------------------------------------------------------------------

function trackpad(root) {
  const surface = h('div', { class: 'pad', role: 'application', 'aria-label': 'Trackpad. One finger moves, tap clicks, two fingers scroll.' },
    h('div', { class: 'pad-hint' }, h('div', null, 'Tap to click · two-finger tap for right click'), h('div', null, 'Two fingers to scroll · double-tap and hold to drag')));
  const btn = (label, button) => {
    const b = h('button', { class: 'pad-btn', 'aria-label': `${label} click` }, label);
    b.addEventListener('pointerdown', (e) => { e.preventDefault(); b.classList.add('down'); queue(['b', button, 'down']); });
    const up = () => { if (b.classList.contains('down')) { b.classList.remove('down'); queue(['b', button, 'up']); } };
    b.addEventListener('pointerup', up);
    b.addEventListener('pointerleave', up);
    b.addEventListener('pointercancel', up);
    return b;
  };
  const buttons = h('div', { class: 'pad-buttons' }, btn('Left', 'left'), btn('Middle', 'middle'), btn('Right', 'right'));
  root.append(surface, buttons);

  let mode = null;
  let start = null;
  let last = null;
  let moved = false;
  let lastTap = { t: 0, x: 0, y: 0 };
  let dragging = false;
  let maxTouches = 0;

  const centroid = (touches) => {
    let x = 0;
    let y = 0;
    for (const t of touches) { x += t.clientX; y += t.clientY; }
    return { x: x / touches.length, y: y / touches.length };
  };

  surface.addEventListener('touchstart', (e) => {
    e.preventDefault();
    const n = e.touches.length;
    maxTouches = Math.max(maxTouches, n);
    const c = centroid(e.touches);
    const now = performance.now();
    if (n === 1) {
      moved = false;
      start = { ...c, t: now };
      last = { ...c, t: now };
      mode = 'move';
      // second touch soon after a tap, in the same spot: tap-and-drag
      if (now - lastTap.t < 300 && Math.hypot(c.x - lastTap.x, c.y - lastTap.y) < 40) mode = 'drag-pending';
    } else if (n === 2) {
      mode = 'scroll';
      start = { ...c, t: now };
      last = { ...c, t: now };
    } else if (n >= 3) {
      mode = 'three';
      start = { ...c, t: now };
      last = { ...c, t: now };
    }
    surface.classList.add('active');
  }, { passive: false });

  surface.addEventListener('touchmove', (e) => {
    e.preventDefault();
    const c = centroid(e.touches);
    const now = performance.now();
    const dx = c.x - last.x;
    const dy = c.y - last.y;
    const dt = Math.max(1, now - last.t);
    if (Math.hypot(c.x - start.x, c.y - start.y) > 6) moved = true;
    if (mode === 'drag-pending' && moved) {
      mode = 'drag';
      dragging = true;
      queue(['b', 'left', 'down']);
    }
    if (mode === 'move' || mode === 'drag') {
      // speed-dependent gain: slow = precise, fast = covers the screen
      const speed = Math.hypot(dx, dy) / dt;
      const gain = prefs.sensitivity * (1 + 1.4 * Math.min(2.5, Math.max(0, speed - 0.12))) * 1.6;
      queue(['m', dx * gain, dy * gain]);
    } else if (mode === 'scroll') {
      queue(['s', Math.round(-dx * 3 * prefs.scrollSpeed), Math.round(dy * 3 * prefs.scrollSpeed)]);
    }
    last = { ...c, t: now };
  }, { passive: false });

  surface.addEventListener('touchend', (e) => {
    e.preventDefault();
    if (e.touches.length) {
      // a finger lifted but others remain; restart deltas from the remaining centroid
      last = { ...centroid(e.touches), t: performance.now() };
      return;
    }
    const now = performance.now();
    const dt = now - (start?.t || now);
    if (mode === 'drag' || dragging) {
      queue(['b', 'left', 'up']);
      dragging = false;
    } else if ((mode === 'move' || mode === 'drag-pending') && !moved && dt < 250 && maxTouches === 1) {
      queue(['b', 'left', 'click']);
      lastTap = { t: now, x: start.x, y: start.y };
      tap(surface);
    } else if (mode === 'scroll' && !moved && dt < 300 && maxTouches === 2) {
      queue(['b', 'right', 'click']);
      tap(surface);
    } else if (mode === 'three') {
      const dx = last.x - start.x;
      const dy = last.y - start.y;
      if (Math.abs(dy) > 50 && Math.abs(dy) > Math.abs(dx)) sendKeys(dy < 0 ? ['win', 'tab'] : ['win', 'd']);
      else if (Math.abs(dx) > 50) sendKeys(['ctrl', 'win', dx < 0 ? 'right' : 'left']);
    }
    mode = null;
    maxTouches = 0;
    surface.classList.remove('active');
  }, { passive: false });

  surface.addEventListener('touchcancel', () => {
    if (dragging) queue(['b', 'left', 'up']);
    dragging = false;
    mode = null;
    maxTouches = 0;
    surface.classList.remove('active');
  });

  // mouse users (desktop browser): plain relative drag
  let mouseDown = null;
  surface.addEventListener('mousedown', (e) => { mouseDown = { x: e.clientX, y: e.clientY, moved: false }; });
  window.addEventListener('mousemove', (e) => {
    if (!mouseDown) return;
    const dx = e.clientX - mouseDown.x;
    const dy = e.clientY - mouseDown.y;
    if (Math.abs(dx) + Math.abs(dy) > 2) mouseDown.moved = true;
    mouseDown.x = e.clientX;
    mouseDown.y = e.clientY;
    queue(['m', dx * prefs.sensitivity, dy * prefs.sensitivity]);
  });
  window.addEventListener('mouseup', () => {
    if (mouseDown && !mouseDown.moved) queue(['b', 'left', 'click']);
    mouseDown = null;
  });
  surface.addEventListener('wheel', (e) => { e.preventDefault(); queue(['s', Math.round(-e.deltaX), Math.round(-e.deltaY)]); }, { passive: false });

  const sens = slider({
    min: 0.5, max: 3, step: 0.1, value: prefs.sensitivity, label: 'Sensitivity',
    onChange: (v) => setPref('sensitivity', v),
  });
  root.append(h('div', { class: 'pad-sens' }, icon('pointer'), sens));
  return {};
}

// --- keyboard ---------------------------------------------------------------------

const SHORTCUTS = [
  { label: 'Alt+Tab', keys: ['alt', 'tab'] },
  { label: 'Copy', keys: ['ctrl', 'c'] },
  { label: 'Paste', keys: ['ctrl', 'v'] },
  { label: 'Cut', keys: ['ctrl', 'x'] },
  { label: 'Undo', keys: ['ctrl', 'z'] },
  { label: 'Select all', keys: ['ctrl', 'a'] },
  { label: 'Desktop', keys: ['win', 'd'] },
  { label: 'Explorer', keys: ['win', 'e'] },
  { label: 'Task Manager', keys: ['ctrl', 'shift', 'esc'] },
  { label: 'Snip', keys: ['win', 'shift', 's'] },
  { label: 'Close window', keys: ['alt', 'f4'] },
  { label: 'New tab', keys: ['ctrl', 't'] },
  { label: 'Close tab', keys: ['ctrl', 'w'] },
  { label: 'Run', keys: ['win', 'r'] },
];

function keyboard(root) {
  const mods = { ctrl: 0, alt: 0, shift: 0, win: 0 }; // 0 off, 1 next key, 2 locked
  const modBtns = {};
  const paintMods = () => {
    for (const [k, b] of Object.entries(modBtns)) {
      b.classList.toggle('latched', mods[k] === 1);
      b.classList.toggle('locked', mods[k] === 2);
      b.setAttribute('aria-pressed', String(mods[k] > 0));
    }
  };
  const activeMods = () => Object.keys(mods).filter((k) => mods[k]);
  const releaseLatched = () => {
    for (const k of Object.keys(mods)) if (mods[k] === 1) mods[k] = 0;
    paintMods();
  };
  const press = (key) => {
    const held = activeMods();
    if (held.length) sendKeys([...held, key]);
    else queue(['k', key, 'press']);
    releaseLatched();
  };

  const modRow = h('div', { class: 'keys mods' }, ['ctrl', 'alt', 'shift', 'win'].map((k) => {
    const b = h('button', { class: 'key mod', 'aria-pressed': 'false' }, k === 'win' ? 'Win' : k[0].toUpperCase() + k.slice(1));
    let lastTapAt = 0;
    b.addEventListener('click', () => {
      const now = Date.now();
      if (mods[k] === 1 && now - lastTapAt < 350) mods[k] = 2;
      else mods[k] = mods[k] ? 0 : 1;
      lastTapAt = now;
      paintMods();
      tap(b);
    });
    modBtns[k] = b;
    return b;
  }));

  const key = (label, name, cls = '') => h('button', { class: `key ${cls}`, 'aria-label': name, onclick: (e) => { press(name); tap(e.currentTarget); } }, label);
  const nav = h('div', { class: 'keys grid4' },
    key('Esc', 'esc'), key('Tab', 'tab'), key(icon('arrow-up'), 'up'), key('Del', 'del'),
    key(icon('arrow-left'), 'left'), key(icon('arrow-down'), 'down'), key(icon('arrow-right'), 'right'), key(icon('enter'), 'enter', 'accent'),
    key('Home', 'home'), key('End', 'end'), key('PgUp', 'pgup'), key('PgDn', 'pgdn'));
  const fkeys = h('div', { class: 'keys frow' }, Array.from({ length: 12 }, (_, i) => key(`F${i + 1}`, `f${i + 1}`)),
    key('PrtSc', 'printscreen'), key('Ins', 'ins'));

  const ta = h('textarea', {
    class: 'textarea', placeholder: 'Type here… it goes to the focused window on the PC',
    autocapitalize: 'off', autocomplete: 'off', autocorrect: 'off', spellcheck: false, rows: 3,
  });
  let live = load('landeck.liveTyping', false);
  const liveToggle = toggle(live, (v) => { live = v; save('landeck.liveTyping', v); ta.value = ''; sendBtn.hidden = v; }, 'Live typing');
  const sendBtn = h('button', { class: 'btn primary', onclick: () => {
    const held = activeMods();
    if (held.length && ta.value.length === 1) press(ta.value.toLowerCase());
    else sendText(ta.value);
    ta.value = '';
    ta.focus();
  } }, icon('send'), 'Send');
  sendBtn.hidden = live;

  ta.addEventListener('beforeinput', (e) => {
    if (!live) return;
    e.preventDefault();
    if (e.inputType === 'insertText' || e.inputType === 'insertReplacementText' || e.inputType === 'insertFromPaste') {
      const text = e.data ?? e.dataTransfer?.getData('text/plain') ?? '';
      const held = activeMods();
      if (held.length && text.length === 1) press(text.toLowerCase());
      else sendText(text);
    } else if (e.inputType === 'insertLineBreak' || e.inputType === 'insertParagraph') press('enter');
    else if (e.inputType === 'deleteContentBackward') press('backspace');
    else if (e.inputType === 'deleteContentForward') press('del');
  });
  ta.addEventListener('keydown', (e) => {
    if (!live && e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); sendBtn.click(); }
  });

  const chips = h('div', { class: 'chips wrap-chips' },
    SHORTCUTS.map((s) => h('button', { class: 'chip', onclick: (e) => { sendKeys(s.keys); tap(e.currentTarget); } }, s.label)),
    h('button', { class: 'chip', 'aria-disabled': 'true', title: 'Blocked: use Lock in Control → Power', onclick: () => toast('Win+L is blocked', { text: 'Use Lock in Control → Power instead.', kind: 'warn' }) }, icon('ban'), 'Win+L'));

  root.append(h('div', { class: 'kb-scroll' },
    h('div', { class: 'card stack' },
      ta,
      h('div', { class: 'row' }, h('label', { class: 'row small dim', style: { gap: '10px' } }, liveToggle, 'Live typing'), h('div', { class: 'spacer' }),
        h('button', { class: 'btn square', 'aria-label': 'Backspace', onclick: () => press('backspace') }, icon('backspace')), sendBtn)),
    h('div', { class: 'section-title' }, h('div', { class: 'eyebrow' }, 'Modifiers'), h('div', { class: 'tiny faint' }, 'tap = next key, double-tap = lock')),
    modRow,
    h('div', { class: 'section-title' }, h('div', { class: 'eyebrow' }, 'Keys')),
    nav,
    fkeys,
    h('div', { class: 'section-title' }, h('div', { class: 'eyebrow' }, 'Shortcuts')),
    chips));
  return { destroy() { if (activeMods().length) releaseLatched(); } };
}

// --- screen ----------------------------------------------------------------------------

const PRESETS = [
  { value: 'saver', label: 'Saver' },
  { value: 'balanced', label: 'Balanced' },
  { value: 'sharp', label: 'Sharp' },
];

function screen(root) {
  if (state.features.screen === false) {
    root.append(disabledCard('screen', 'Screen viewing'));
    return {};
  }
  let monitors = [];
  let monitor = load('landeck.monitor', 1);
  let streaming = false;
  let wantStream = false;
  let interactive = load('landeck.interactive', false);
  let frames = 0;
  let fpsShown = 0;
  let lastFrameAt = 0;
  let hideTimer = null;
  let frameSize = { w: 0, h: 0 };
  const view = { scale: 1, x: 0, y: 0 };

  const canvas = h('canvas', { class: 'screen-canvas', 'aria-label': 'PC screen' });
  const ctx2d = canvas.getContext('2d');
  const stage = h('div', { class: 'screen-stage' }, canvas);
  const placeholder = h('div', { class: 'screen-placeholder' },
    h('div', { class: 'empty-icon' }, icon('monitor')),
    h('p', { class: 'dim' }, 'Live view of the PC screen.'),
    h('p', { class: 'small faint' }, 'Pinch to zoom, drag to pan. Turn on Tap to click to control it directly.'));
  const stats = h('div', { class: 'screen-stats num', hidden: true });
  const exitFull = h('button', { class: 'icon-btn screen-exit', 'aria-label': 'Exit full screen', onclick: () => setFull(false) }, icon('minimize'));
  const viewer = h('div', { class: 'screen-viewer' }, placeholder, stage, stats, exitFull);

  const startBtn = h('button', { class: 'btn primary' });
  const monitorChips = h('div', { class: 'chips' });
  const presetSeg = segmented(PRESETS, prefs.screenPreset, (v) => { setPref('screenPreset', v); if (streaming) startStream(); }, { label: 'Quality' });
  const fpsLabel = h('span', { class: 'num' }, `${prefs.screenFps} fps`);
  const fps = slider({ min: 1, max: 15, step: 1, value: prefs.screenFps, label: 'Frames per second',
    onInput: (v) => { fpsLabel.textContent = `${v} fps`; },
    onChange: (v) => { setPref('screenFps', v); if (streaming) startStream(); } });
  const interToggle = toggle(interactive, (v) => { interactive = v; save('landeck.interactive', v); viewer.classList.toggle('interactive', v); }, 'Interactive');
  viewer.classList.toggle('interactive', interactive);

  startBtn.style.flex = '1';
  const controls = h('div', { class: 'screen-controls stack' },
    h('div', { class: 'row' }, startBtn,
      h('button', { class: 'btn square', 'aria-label': 'Full screen', onclick: () => setFull(true) }, icon('maximize')),
      h('button', { class: 'btn square', 'aria-label': 'Screenshot', onclick: screenshot }, icon('camera'))),
    monitorChips,
    presetSeg,
    h('div', { class: 'row' },
      h('span', { class: 'small dim num', style: { width: '48px', flex: 'none' } }, fpsLabel), h('div', { style: { flex: 1 } }, fps),
      h('label', { class: 'row small dim', style: { gap: '8px', flex: 'none' } }, 'Tap to click', interToggle)));

  root.append(h('div', { class: 'screen-wrap' }, viewer, controls));

  function paintStart() {
    swap(startBtn, icon(streaming ? 'stop' : 'play'), streaming ? 'Stop' : 'Start live view');
    startBtn.classList.toggle('primary', !streaming);
    startBtn.classList.toggle('danger', streaming);
  }
  paintStart();
  startBtn.addEventListener('click', () => (streaming ? stopStream(true) : startStream()));

  function renderMonitors() {
    swap(monitorChips, monitors.length > 1 ? monitors.map((m) => h('button', {
      class: 'chip', 'aria-pressed': String(m.index === monitor),
      onclick: () => { monitor = m.index; save('landeck.monitor', monitor); renderMonitors(); if (streaming) startStream(); },
    }, icon('monitor'), `${m.index}${m.primary ? ' (main)' : ''} · ${m.width}×${m.height}`)) : []);
    monitorChips.hidden = monitors.length <= 1;
  }

  async function loadMonitors() {
    try {
      ({ monitors } = await api.get('/screen/monitors'));
      if (!monitors.find((m) => m.index === monitor)) monitor = monitors[0]?.index || 1;
      renderMonitors();
    } catch { /* offline; retried on reconnect */ }
  }

  function startStream() {
    wantStream = true;
    const ok = conn.send({ type: 'screen.start', monitor, fps: prefs.screenFps, preset: prefs.screenPreset, cursor: true });
    if (!ok) { toast('Not connected', { text: 'Live view starts once the PC is back.', kind: 'warn' }); return; }
    streaming = true;
    placeholder.hidden = true;
    stats.hidden = false;
    paintStart();
  }

  function stopStream(user = false) {
    if (user) wantStream = false;
    conn.send({ type: 'screen.stop' });
    streaming = false;
    paintStart();
    stats.hidden = true;
  }

  const headerLen = 20;
  const offFrame = conn.on('frame', async (buf) => {
    if (!streaming) return;
    const dv = new DataView(buf);
    if (buf.byteLength < headerLen || dv.getUint32(0, false) !== 0x4c444631) return; // "LDF1"
    const seq = dv.getUint32(4, true);
    const t = dv.getFloat64(8, true);
    const w = dv.getUint16(16, true);
    const hgt = dv.getUint16(18, true);
    const blob = new Blob([new Uint8Array(buf, headerLen)], { type: 'image/jpeg' });
    try {
      const bmp = await createImageBitmap(blob);
      if (canvas.width !== bmp.width || canvas.height !== bmp.height) {
        canvas.width = bmp.width;
        canvas.height = bmp.height;
      }
      ctx2d.drawImage(bmp, 0, 0);
      bmp.close?.();
      frameSize = { w, h: hgt };
      frames++;
      lastFrameAt = performance.now();
    } catch { /* corrupt frame, skip */ }
    conn.send({ type: 'screen.ack', seq, t });
  });

  let rtt = null;
  const offStats = conn.on('screen.stats', (s) => { rtt = s.rtt; stats.dataset.kb = Math.round(s.bytes / 1024); });
  const offStopped = conn.on('screen.stopped', () => { streaming = false; paintStart(); stats.hidden = true; });
  const offErr = conn.on('screen.error', (e) => { streaming = false; wantStream = false; paintStart(); toast('Live view stopped', { text: e.detail, kind: 'error' }); });
  const offOnline = conn.on('online', () => { loadMonitors(); if (wantStream) startStream(); });
  const offClosed = conn.on('closed', () => { streaming = false; paintStart(); });

  const statTimer = setInterval(() => {
    fpsShown = frames;
    frames = 0;
    const stale = streaming && performance.now() - lastFrameAt > 3000;
    stats.textContent = stale ? 'waiting for frames…'
      : `${fpsShown} fps · ${rtt != null ? `${Math.round(rtt)} ms` : '–'} · ${canvas.width}×${canvas.height} · ${stats.dataset.kb || 0} KB`;
  }, 1000);

  const onVisibility = () => {
    if (document.visibilityState === 'hidden') {
      hideTimer = setTimeout(() => { if (streaming) stopStream(false); }, 10000);
    } else {
      clearTimeout(hideTimer);
      if (wantStream && !streaming) startStream();
    }
  };
  document.addEventListener('visibilitychange', onVisibility);

  // --- zoom, pan, taps -----------------------------------------------------------------

  const applyView = () => {
    stage.style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.scale})`;
  };
  const clampView = () => {
    view.scale = Math.min(6, Math.max(1, view.scale));
    if (view.scale === 1) { view.x = 0; view.y = 0; }
    const r = viewer.getBoundingClientRect();
    const maxX = (r.width * (view.scale - 1)) / 2;
    const maxY = (r.height * (view.scale - 1)) / 2;
    view.x = Math.min(maxX, Math.max(-maxX, view.x));
    view.y = Math.min(maxY, Math.max(-maxY, view.y));
  };

  function toPc(clientX, clientY) {
    const rect = canvas.getBoundingClientRect();
    const mon = monitors.find((m) => m.index === monitor);
    if (!mon || !rect.width) return null;
    const fx = (clientX - rect.left) / rect.width;
    const fy = (clientY - rect.top) / rect.height;
    if (fx < 0 || fy < 0 || fx > 1 || fy > 1) return null;
    return [Math.round(mon.left + fx * mon.width), Math.round(mon.top + fy * mon.height)];
  }

  let g = null;
  let lastTapAt = 0;
  let pressTimer = null;
  viewer.addEventListener('touchstart', (e) => {
    if (e.target.closest('button')) return;
    e.preventDefault();
    const ts = e.touches;
    if (ts.length === 1) {
      g = { mode: 'one', x: ts[0].clientX, y: ts[0].clientY, sx: ts[0].clientX, sy: ts[0].clientY, t: performance.now(), moved: false };
      if (interactive && streaming) {
        pressTimer = setTimeout(() => {
          const p = toPc(g.sx, g.sy);
          if (p && g && !g.moved) { queue(['a', ...p]); queue(['b', 'right', 'click']); tap(viewer); g.mode = 'done'; }
        }, 550);
      }
    } else if (ts.length === 2) {
      clearTimeout(pressTimer);
      const d = Math.hypot(ts[0].clientX - ts[1].clientX, ts[0].clientY - ts[1].clientY);
      g = { mode: 'pinch', d, scale: view.scale, cx: (ts[0].clientX + ts[1].clientX) / 2, cy: (ts[0].clientY + ts[1].clientY) / 2, x: view.x, y: view.y };
    }
  }, { passive: false });

  viewer.addEventListener('touchmove', (e) => {
    if (!g) return;
    e.preventDefault();
    const ts = e.touches;
    if (g.mode === 'one' && ts.length === 1) {
      const dx = ts[0].clientX - g.x;
      const dy = ts[0].clientY - g.y;
      if (Math.hypot(ts[0].clientX - g.sx, ts[0].clientY - g.sy) > 8) { g.moved = true; clearTimeout(pressTimer); }
      if (view.scale > 1) { view.x += dx; view.y += dy; clampView(); applyView(); }
      g.x = ts[0].clientX;
      g.y = ts[0].clientY;
    } else if (g.mode === 'pinch' && ts.length === 2) {
      const d = Math.hypot(ts[0].clientX - ts[1].clientX, ts[0].clientY - ts[1].clientY);
      const cx = (ts[0].clientX + ts[1].clientX) / 2;
      const cy = (ts[0].clientY + ts[1].clientY) / 2;
      view.scale = g.scale * (d / g.d);
      view.x = g.x + (cx - g.cx);
      view.y = g.y + (cy - g.cy);
      clampView();
      applyView();
    }
  }, { passive: false });

  viewer.addEventListener('touchend', (e) => {
    clearTimeout(pressTimer);
    if (!g || e.touches.length) return;
    if (g.mode === 'one' && !g.moved && performance.now() - g.t < 300) {
      const now = performance.now();
      if (interactive && streaming) {
        const p = toPc(g.sx, g.sy);
        if (p) { queue(['a', ...p]); queue(['b', 'left', 'click']); tap(viewer); }
      } else if (now - lastTapAt < 320) {
        // double tap toggles zoom
        view.scale = view.scale > 1 ? 1 : 2.5;
        clampView();
        applyView();
      }
      lastTapAt = now;
    }
    g = null;
  });

  viewer.addEventListener('click', (e) => {
    // desktop browsers
    if (!interactive || !streaming || e.target.closest('button') || 'ontouchstart' in window) return;
    const p = toPc(e.clientX, e.clientY);
    if (p) { queue(['a', ...p]); queue(['b', 'left', 'click']); }
  });

  function setFull(on) {
    document.body.classList.toggle('screen-full', on);
    viewer.classList.toggle('full', on);
    if (on && viewer.requestFullscreen && !/iPhone/.test(navigator.userAgent)) viewer.requestFullscreen().catch(() => {});
    if (!on && document.fullscreenElement) document.exitFullscreen().catch(() => {});
    view.scale = 1;
    clampView();
    applyView();
    if (on) toast('Rotate your phone for a bigger picture', { ico: 'phone', timeout: 2200 });
  }

  async function screenshot() {
    const sh = sheetShot();
    try {
      const blob = await api.blob(`/screen/shot?monitor=${monitor}&q=85`);
      sh.show(URL.createObjectURL(blob));
    } catch (err) {
      sh.fail(err);
    }
  }

  function sheetShot() {
    const img = h('img', { class: 'shot-img', alt: 'Screenshot of the PC' });
    const body = h('div', { class: 'stack' }, h('div', { class: 'skel', style: { aspectRatio: '16/9', borderRadius: '14px' } }));
    const saveBtn = h('button', { class: 'btn', onclick: async () => {
      try {
        const r = await api.post('/screen/shot/save', { monitor });
        toast('Saved on the PC', { text: r.name, kind: 'ok' });
      } catch (err) { errorToast(err); }
    } }, icon('download'), 'Save to PC Downloads');
    sheet({ title: 'Screenshot', body, foot: h('div', { class: 'stack' }, h('p', { class: 'small faint' }, 'Press and hold the image to save it to Photos.'), saveBtn) });
    return {
      show(src) { img.src = src; swap(body, img); },
      fail(err) { swap(body, h('div', { class: 'notice danger' }, icon('alert'), err.detail || 'Screenshot failed.')); },
    };
  }

  loadMonitors();
  return {
    refresh() { loadMonitors(); },
    destroy() {
      if (streaming) stopStream(true);
      setFull(false);
      clearInterval(statTimer);
      clearTimeout(hideTimer);
      document.removeEventListener('visibilitychange', onVisibility);
      [offFrame, offStats, offStopped, offErr, offOnline, offClosed].forEach((f) => f());
    },
  };
}

// --- host ---------------------------------------------------------------------------------

const PANES = { trackpad, keyboard, screen };

export default function remote(root, { match }) {
  if (state.features.input === false && state.features.screen === false) {
    root.append(disabledCard('input', 'Remote control'));
    return {};
  }
  let sub = match[1] || load('landeck.remoteTab', 'trackpad');
  const seg = segmented([
    { value: 'trackpad', label: 'Trackpad', icon: 'mouse' },
    { value: 'keyboard', label: 'Keyboard', icon: 'keyboard' },
    { value: 'screen', label: 'Screen', icon: 'monitor' },
  ], sub, (v) => navigate(`/remote/${v}`, { replace: true }), { label: 'Remote mode' });
  const pane = h('div', { class: 'remote-pane' });
  root.append(seg, pane);
  let inst = null;

  const show = (name) => {
    inst?.destroy?.();
    swap(pane);
    sub = name;
    save('landeck.remoteTab', name);
    seg.select(name);
    pane.dataset.pane = name;
    if (name !== 'screen' && state.features.input === false) {
      pane.append(disabledCard('input', 'Remote input'));
      inst = {};
      return;
    }
    inst = PANES[name](pane) || {};
  };
  show(sub);

  return {
    update({ match: m }) { const next = m[1] || sub; if (next !== sub) show(next); },
    refresh: (i) => inst?.refresh?.(i),
    destroy: () => inst?.destroy?.(),
  };
}

