import { icon } from './icons.js';

// --- DOM ------------------------------------------------------------------

export function h(tag, props, ...children) {
  const el = tag.includes(':svg') ? document.createElementNS('http://www.w3.org/2000/svg', tag.replace(':svg', ''))
    : document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v == null || v === false) continue;
      if (k === 'class') el.setAttribute('class', v);
      else if (k === 'style' && typeof v === 'object') {
        for (const [prop, val] of Object.entries(v)) {
          if (prop.startsWith('--')) el.style.setProperty(prop, val);
          else el.style[prop] = val;
        }
      }
      else if (k === 'ref') v(el);
      else if (k === 'dataset') Object.assign(el.dataset, v);
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
      else if (k === 'html') el.innerHTML = v;
      else if (k in el && typeof v !== 'string' && k !== 'list') el[k] = v;
      else el.setAttribute(k, v === true ? '' : v);
    }
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export function clear(el) {
  while (el.firstChild) el.firstChild.remove();
  return el;
}

export function swap(el, ...children) {
  clear(el);
  return append(el, children);
}

export { icon };

// --- formatting -----------------------------------------------------------

export function fmtBytes(n, digits = 1) {
  if (n == null || isNaN(n)) return '–';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let v = Math.abs(n);
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(i === 0 || v >= 100 ? 0 : digits)} ${units[i]}`;
}

export const fmtRate = (n) => `${fmtBytes(n)}/s`;

export function fmtDuration(sec) {
  if (sec == null || !isFinite(sec)) return '–';
  sec = Math.max(0, Math.round(sec));
  const d = Math.floor(sec / 86400);
  const hrs = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  if (d) return `${d}d ${hrs}h`;
  if (hrs) return `${hrs}h ${m}m`;
  if (m) return `${m}m ${s.toString().padStart(2, '0')}s`;
  return `${s}s`;
}

export function fmtClock(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const hrs = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const mm = m.toString().padStart(hrs ? 2 : 1, '0');
  return hrs ? `${hrs}:${mm}:${s.toString().padStart(2, '0')}` : `${mm}:${s.toString().padStart(2, '0')}`;
}

export function fmtAgo(ts) {
  if (!ts) return 'never';
  const s = Date.now() / 1000 - ts;
  if (s < 45) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  if (s < 86400 * 7) return `${Math.round(s / 86400)} d ago`;
  return new Date(ts * 1000).toLocaleDateString();
}

export function fmtDate(ts) {
  const d = new Date(ts * 1000);
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  return sameDay ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    : d.toLocaleDateString([], { month: 'short', day: 'numeric', year: d.getFullYear() === today.getFullYear() ? undefined : 'numeric' });
}

// --- motion ---------------------------------------------------------------

const tweens = new WeakMap();

export function tween(el, to, fmt = (v) => Math.round(v), dur = 450) {
  const prev = tweens.get(el);
  const from = prev ? prev.value : to;
  if (prev?.raf) cancelAnimationFrame(prev.raf);
  const rec = { value: from, raf: 0 };
  tweens.set(el, rec);
  if (document.documentElement.dataset.motion === 'off' || from === to) {
    rec.value = to;
    el.textContent = fmt(to);
    return;
  }
  const t0 = performance.now();
  const step = (t) => {
    const k = Math.min(1, (t - t0) / dur);
    const e = 1 - Math.pow(1 - k, 3);
    rec.value = from + (to - from) * e;
    el.textContent = fmt(rec.value);
    if (k < 1) rec.raf = requestAnimationFrame(step);
  };
  rec.raf = requestAnimationFrame(step);
}

export function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

export function throttle(fn, ms) {
  let last = 0;
  let t;
  return (...a) => {
    const now = Date.now();
    clearTimeout(t);
    if (now - last >= ms) { last = now; fn(...a); } else t = setTimeout(() => { last = Date.now(); fn(...a); }, ms - (now - last));
  };
}

export function tap(el) {
  // tiny scale bump, our stand-in for haptics on iOS (no vibrate API there)
  if (document.documentElement.dataset.motion === 'off') return;
  el.animate?.([{ transform: 'scale(1)' }, { transform: 'scale(.94)' }, { transform: 'scale(1)' }], { duration: 160, easing: 'ease-out' });
}

// --- toasts ---------------------------------------------------------------

let toastHost;

export function toast(title, { text = '', kind = 'info', ico, timeout = 3200, action } = {}) {
  if (!toastHost) {
    toastHost = h('div', { class: 'toasts', role: 'status', 'aria-live': 'polite' });
    document.body.append(toastHost);
  }
  const icons = { info: 'info', ok: 'check', warn: 'alert', error: 'alert' };
  const el = h('div', { class: `toast ${kind}` },
    icon(ico || icons[kind] || 'info'),
    h('div', { class: 'toast-body' }, h('div', { class: 'toast-title' }, title), text && h('div', { class: 'toast-text' }, text)),
    action && h('button', { class: 'btn small ghost', onclick: () => { action.run(); close(); } }, action.label),
  );
  const close = () => {
    if (!el.isConnected) return;
    el.classList.add('out');
    setTimeout(() => el.remove(), 220);
  };
  el.addEventListener('click', (e) => { if (!e.target.closest('button')) close(); });
  toastHost.prepend(el);
  while (toastHost.children.length > 3) toastHost.lastChild.remove();
  if (timeout) setTimeout(close, timeout);
  return close;
}

export function errorToast(err, title = 'That didn’t work') {
  const text = err?.detail || err?.message || 'Unknown error';
  if (err?.status === 0) return toast('PC unreachable', { text: 'Waiting for the connection to come back.', kind: 'warn' });
  return toast(title, { text, kind: 'error', timeout: 4500 });
}

// --- bottom sheet ---------------------------------------------------------

export function sheet({ title, body, foot, onClose, className = '' } = {}) {
  const scrim = h('div', { class: 'sheet-scrim' });
  const content = h('div', { class: 'sheet-body' }, body);
  const closeBtn = h('button', { class: 'icon-btn', 'aria-label': 'Close' }, icon('x'));
  const head = h('div', { class: 'sheet-head' }, h('div', { class: 'h2 ellipsis' }, title || ''), closeBtn);
  const grip = h('div', { class: 'sheet-grip' });
  const el = h('div', { class: `sheet ${className}`, role: 'dialog', 'aria-modal': 'true', 'aria-label': title || 'Details' },
    grip, head, content, foot && h('div', { class: 'sheet-body', style: { paddingTop: '0' } }, foot));
  document.body.append(scrim, el);
  const prevFocus = document.activeElement;

  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    el.classList.remove('open');
    scrim.classList.remove('open');
    setTimeout(() => { el.remove(); scrim.remove(); }, 320);
    document.removeEventListener('keydown', onKey);
    onClose?.();
    prevFocus?.focus?.();
  };
  const onKey = (e) => { if (e.key === 'Escape') close(); };
  document.addEventListener('keydown', onKey);
  scrim.addEventListener('click', close);
  closeBtn.addEventListener('click', close);

  // drag down to dismiss (from the grip/header, or the body when it's scrolled to the top)
  let y0 = null;
  let dy = 0;
  const start = (e) => {
    if (e.target.closest('input, textarea, .range, button:not(.icon-btn)') && !e.target.closest('.sheet-grip')) return;
    if (e.currentTarget === content && content.scrollTop > 0) return;
    y0 = e.touches[0].clientY;
    dy = 0;
  };
  const move = (e) => {
    if (y0 == null) return;
    dy = Math.max(0, e.touches[0].clientY - y0);
    if (dy > 4) {
      el.classList.add('dragging');
      el.style.transform = `translateY(${dy}px)`;
      if (e.cancelable) e.preventDefault();
    }
  };
  const end = () => {
    if (y0 == null) return;
    el.classList.remove('dragging');
    el.style.transform = '';
    if (dy > 110) close();
    y0 = null;
  };
  for (const target of [grip, head, content]) {
    target.addEventListener('touchstart', start, { passive: true });
    target.addEventListener('touchmove', move, { passive: false });
    target.addEventListener('touchend', end);
  }

  requestAnimationFrame(() => requestAnimationFrame(() => {
    el.classList.add('open');
    scrim.classList.add('open');
    (el.querySelector('[autofocus]') || closeBtn).focus({ preventScroll: true });
  }));
  return { el, body: content, close, setTitle: (t) => { head.firstChild.textContent = t; } };
}

// --- hold to confirm --------------------------------------------------------

export function holdButton({ label, ico, kind = 'danger', duration = 1200, onConfirm, className = '' }) {
  const fill = h('i', { class: 'hold-fill' });
  const ring = h('svg:svg', { class: 'hold-ring', viewBox: '0 0 22 22' });
  ring.innerHTML = '<circle class="bg" cx="11" cy="11" r="9"/><circle class="fg" cx="11" cy="11" r="9"/>';
  const btn = h('button', { class: `btn hold ${kind} ${className}`, style: { '--hold': `${duration}ms` }, 'aria-label': `${label} (press and hold)` },
    fill, ico ? icon(ico) : ring, h('span', null, label));
  let timer = null;
  const cancel = () => {
    clearTimeout(timer);
    timer = null;
    btn.classList.remove('holding');
  };
  const begin = (e) => {
    if (btn.disabled) return;
    if (e.type === 'keydown' && e.key !== 'Enter' && e.key !== ' ') return;
    if (e.type === 'keydown' && e.repeat) return;
    e.preventDefault();
    btn.classList.add('holding');
    timer = setTimeout(() => {
      cancel();
      tap(btn);
      onConfirm?.();
    }, duration);
  };
  btn.addEventListener('pointerdown', begin);
  btn.addEventListener('keydown', begin);
  for (const ev of ['pointerup', 'pointerleave', 'pointercancel', 'keyup', 'blur']) btn.addEventListener(ev, cancel);
  btn.addEventListener('contextmenu', (e) => e.preventDefault());
  return btn;
}

// --- form bits ------------------------------------------------------------

export function segmented(options, value, onChange, { label = 'Options' } = {}) {
  const ink = h('i', { class: 'seg-ink' });
  const el = h('div', { class: 'seg', role: 'tablist', 'aria-label': label }, ink);
  const buttons = options.map((o) => {
    const b = h('button', { role: 'tab', 'aria-selected': String(o.value === value), onclick: () => select(o.value, true) },
      o.icon && icon(o.icon), o.label);
    b.dataset.value = o.value;
    el.append(b);
    return b;
  });
  const place = () => {
    const cur = buttons.find((b) => b.getAttribute('aria-selected') === 'true');
    if (!cur) return;
    ink.style.width = `${cur.offsetWidth}px`;
    ink.style.transform = `translateX(${cur.offsetLeft}px)`;
  };
  const select = (v, user) => {
    buttons.forEach((b) => b.setAttribute('aria-selected', String(b.dataset.value === String(v))));
    place();
    if (user) onChange?.(v);
  };
  new ResizeObserver(place).observe(el);
  requestAnimationFrame(place);
  el.select = (v) => select(v, false);
  return el;
}

export function toggle(checked, onChange, label = '') {
  const el = h('button', { class: 'switch', role: 'switch', 'aria-checked': String(!!checked), 'aria-label': label });
  el.addEventListener('click', () => {
    const next = el.getAttribute('aria-checked') !== 'true';
    el.setAttribute('aria-checked', String(next));
    tap(el);
    onChange?.(next, () => el.setAttribute('aria-checked', String(!next)));
  });
  el.set = (v) => el.setAttribute('aria-checked', String(!!v));
  return el;
}

export function slider({ min = 0, max = 100, step = 1, value = 0, label = 'Value', onInput, onChange, big = false }) {
  const input = h('input', { type: 'range', min, max, step, value, 'aria-label': label });
  const fill = h('i', { class: 'range-fill' });
  const thumb = h('i', { class: 'range-thumb' });
  const el = h('div', { class: `range${big ? ' big' : ''}` }, h('div', { class: 'range-track' }, fill), thumb, input);
  const paint = () => {
    const pct = ((input.value - min) / (max - min)) * 100;
    fill.style.width = `${pct}%`;
    thumb.style.left = `calc(13px + (100% - 26px) * ${pct / 100})`;
  };
  input.addEventListener('input', () => { paint(); onInput?.(Number(input.value)); });
  input.addEventListener('change', () => onChange?.(Number(input.value)));
  paint();
  el.input = input;
  el.set = (v) => { if (document.activeElement !== input) { input.value = v; paint(); } };
  return el;
}

// --- placeholders ---------------------------------------------------------

export function skeletonList(n = 6) {
  return h('div', { class: 'list', 'aria-busy': 'true' },
    Array.from({ length: n }, () => h('div', { class: 'skel-row' },
      h('div', { class: 'skel ic' }), h('div', { class: 'lines' }, h('div', { class: 'skel l1' }), h('div', { class: 'skel l2' })))));
}

export function skeletonBlock(height = 120, radius = 20) {
  return h('div', { class: 'skel', style: { height: `${height}px`, borderRadius: `${radius}px` } });
}

export function empty({ ico = 'info', text, sub, action }) {
  return h('div', { class: 'empty' },
    h('div', { class: 'empty-icon' }, icon(ico)),
    h('p', null, text),
    sub && h('p', { class: 'small faint' }, sub),
    action && h('button', { class: 'btn primary small', onclick: action.run }, action.label));
}

export function disabledCard(feature, what) {
  return h('div', { class: 'card' }, empty({
    ico: 'ban',
    text: `${what} is turned off on the PC.`,
    sub: 'Open the LanDeck admin page on the PC (tray icon → Open admin) to switch it on.',
  }));
}

// --- clipboard on the phone -------------------------------------------------

export async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch { /* fall through to the old trick */ }
  const ta = h('textarea', { style: { position: 'fixed', top: '-1000px', opacity: '0' }, readonly: true });
  ta.value = text;
  document.body.append(ta);
  ta.select();
  ta.setSelectionRange(0, text.length);
  let ok = false;
  try { ok = document.execCommand('copy'); } catch { ok = false; }
  ta.remove();
  return ok;
}

// --- gestures ---------------------------------------------------------------

export function pullToRefresh(scroller, onRefresh) {
  const ind = h('div', { class: 'ptr', 'aria-hidden': 'true' }, icon('refresh'));
  scroller.prepend(ind);
  let y0 = null;
  let dist = 0;
  let busy = false;
  scroller.addEventListener('touchstart', (e) => {
    if (scroller.scrollTop > 0 || busy) return;
    y0 = e.touches[0].clientY;
    dist = 0;
  }, { passive: true });
  scroller.addEventListener('touchmove', (e) => {
    if (y0 == null) return;
    dist = Math.max(0, e.touches[0].clientY - y0);
    if (dist > 0 && scroller.scrollTop <= 0) {
      const d = Math.min(90, dist * 0.5);
      ind.style.transform = `translateY(${d}px) rotate(${d * 4}deg)`;
      ind.style.opacity = String(Math.min(1, d / 50));
    }
  }, { passive: true });
  scroller.addEventListener('touchend', async () => {
    if (y0 == null) return;
    y0 = null;
    if (dist * 0.5 > 60) {
      busy = true;
      ind.classList.add('spinning');
      try { await onRefresh(); } finally {
        busy = false;
        ind.classList.remove('spinning');
      }
    }
    ind.style.transform = '';
    ind.style.opacity = '0';
  });
}

export function swipeRow(item, actions) {
  // actions: [{label, ico, cls, run}] revealed from the right
  const width = actions.length * 76;
  const bar = h('div', { class: 'swipe-actions', style: { width: `${width}px` } },
    actions.map((a) => h('button', { class: a.cls || 'alt', 'aria-label': a.label, onclick: (e) => { e.stopPropagation(); reset(); a.run(); } },
      icon(a.ico), a.label)));
  const wrap = h('div', { class: 'swipe' }, bar, item);
  let x0 = null;
  let y0 = 0;
  let dx = 0;
  let open = false;
  let locked = null;
  const reset = () => { open = false; item.style.transform = ''; };
  item.addEventListener('touchstart', (e) => {
    x0 = e.touches[0].clientX;
    y0 = e.touches[0].clientY;
    locked = null;
    dx = open ? -width : 0;
  }, { passive: true });
  item.addEventListener('touchmove', (e) => {
    if (x0 == null) return;
    const mx = e.touches[0].clientX - x0;
    const my = e.touches[0].clientY - y0;
    if (locked == null && Math.hypot(mx, my) > 8) locked = Math.abs(mx) > Math.abs(my) ? 'x' : 'y';
    if (locked !== 'x') return;
    wrap.classList.add('dragging');
    dx = Math.min(0, Math.max(-width - 30, (open ? -width : 0) + mx));
    item.style.transform = `translateX(${dx}px)`;
  }, { passive: true });
  item.addEventListener('touchend', () => {
    wrap.classList.remove('dragging');
    if (locked === 'x') {
      open = dx < -width / 2;
      item.style.transform = open ? `translateX(${-width}px)` : '';
    }
    x0 = null;
  });
  item.addEventListener('click', (e) => {
    if (open) { e.preventDefault(); e.stopImmediatePropagation(); reset(); }
  }, true);
  return wrap;
}

export function kindIcon(kind) {
  return { dir: 'folder', image: 'image', video: 'film', audio: 'music', pdf: 'file-text', archive: 'archive',
    code: 'code', text: 'file-text', doc: 'file-text', exe: 'apps' }[kind] || 'file';
}
