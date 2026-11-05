import { api } from './api.js';
import { brandMark, icon } from './icons.js';
import * as router from './router.js';
import { load, prefs, save, set, state, watch, watchPref } from './store.js';
import { fmtAgo, h, segmented, sheet, swap, toast } from './ui.js';
import { conn } from './ws.js';
import { copyFromPc, flushOutbox } from './clip.js';

import actionsView from './views/actions.js';
import clipboardView from './views/clipboard.js';
import dashboardView from './views/dashboard.js';
import filesView from './views/files.js';
import mediaView from './views/media.js';
import moreView from './views/more.js';
import { showOnboarding, showPair } from './views/pair.js';
import powerView from './views/power.js';
import processesView, { appsView } from './views/processes.js';
import remoteView from './views/remote.js';
import settingsView from './views/settings.js';
import terminalView from './views/terminal.js';

const TABS = [
  { id: 'home', label: 'Home', icon: 'home', path: '/home' },
  { id: 'remote', label: 'Remote', icon: 'pointer', path: '/remote' },
  { id: 'files', label: 'Files', icon: 'folder', path: '/files' },
  { id: 'control', label: 'Control', icon: 'sliders', path: '/control' },
  { id: 'more', label: 'More', icon: 'grid', path: '/more' },
];

const CONN_LABEL = {
  connecting: 'Connecting',
  online: 'Online',
  reconnecting: 'Reconnecting',
  offline: 'Offline',
  waiting: 'Waiting for PC',
};

// --- theme ------------------------------------------------------------------

const darkQuery = matchMedia('(prefers-color-scheme: dark)');

function applyTheme() {
  const root = document.documentElement;
  const theme = prefs.theme === 'system' ? (darkQuery.matches ? 'dark' : 'light') : prefs.theme;
  root.dataset.theme = theme;
  root.dataset.accent = prefs.accent;
  root.dataset.motion = prefs.motion ? 'on' : 'off';
  const color = { dark: '#07090d', amoled: '#000000', light: '#f3f5f8' }[theme];
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', color);
}

darkQuery.addEventListener?.('change', applyTheme);
watchPref('theme', applyTheme);
watchPref('accent', applyTheme);
watchPref('motion', applyTheme);
applyTheme();

// --- routes -----------------------------------------------------------------

function control(sub) {
  return (el, p) => {
    const views = { actions: actionsView, media: mediaView, power: powerView };
    return views[sub](el, p);
  };
}

router.route(/^\/home$/, dashboardView, { tab: 'home', title: 'Dashboard' });
router.route(/^\/remote(?:\/(trackpad|keyboard|screen))?$/, remoteView, { tab: 'remote', fill: true, title: 'Remote' });
router.route(/^\/files(?:\/(.*))?$/, filesView, { tab: 'files', title: 'Files' });
router.route(/^\/control(?:\/(actions|media|power))?$/, (el, p) => controlHost(el, p), { tab: 'control', title: 'Control' });
router.route(/^\/more$/, moreView, { tab: 'more', title: 'More' });
router.route(/^\/clipboard$/, clipboardView, { tab: 'more', title: 'Clipboard' });
router.route(/^\/processes$/, processesView, { tab: 'more', title: 'Processes' });
router.route(/^\/apps$/, appsView, { tab: 'more', title: 'Apps' });
router.route(/^\/terminal$/, terminalView, { tab: 'more', fill: true, title: 'Terminal' });
router.route(/^\/settings$/, settingsView, { tab: 'more', title: 'Settings' });

// Control tab: one segmented header, three panes
function controlHost(el, { match }) {
  let sub = match[1] || load('landeck.controlTab', 'actions');
  const wrap = h('div', { class: 'wrap' });
  const pane = h('div');
  let inst = null;
  const seg = segmented([
    { value: 'actions', label: 'Actions', icon: 'zap' },
    { value: 'media', label: 'Media', icon: 'music' },
    { value: 'power', label: 'Power', icon: 'power' },
  ], sub, (v) => router.navigate(`/control/${v}`, { replace: true }), { label: 'Control sections' });
  seg.classList.add('sticky-seg');
  wrap.append(seg, pane);
  el.append(wrap);
  const show = (name) => {
    inst?.destroy?.();
    swap(pane);
    sub = name;
    save('landeck.controlTab', name);
    seg.select(name);
    inst = control(name)(pane, {}) || {};
  };
  show(sub);
  return {
    update({ match: m }) { const next = m[1] || sub; if (next !== sub) show(next); },
    refresh: (i) => inst?.refresh?.(i),
    destroy: () => inst?.destroy?.(),
  };
}

// --- shell ------------------------------------------------------------------

function buildShell() {
  const pill = h('button', { class: 'pill', 'aria-live': 'polite', onclick: () => { conn.now(); toast('Reconnecting…', { timeout: 1500 }); } },
    h('i', { class: 'dot' }), h('span', { class: 'pill-text' }, 'Connecting'));
  const badge = h('span', { class: 'badge' });
  const bell = h('button', { class: 'icon-btn', 'aria-label': 'Notifications', onclick: openNotifications }, icon('bell'), badge);
  const name = h('div', { class: 'brand-name' }, state.server?.name || 'LanDeck');
  const sub = h('div', { class: 'brand-sub' }, 'LanDeck');
  const top = h('header', { class: 'topbar' },
    h('div', { class: 'brand' }, brandMark(), h('div', { class: 'brand-text' }, name, sub)),
    pill, bell);

  const ink = h('i', { class: 'tab-ink' });
  const tabs = TABS.map((t) => h('a', { class: 'tab', href: `#${t.path}`, dataset: { tab: t.id } }, icon(t.icon), h('span', null, t.label)));
  const nav = h('nav', { class: 'tabbar', 'aria-label': 'Sections' },
    h('div', { class: 'side-head' }, brandMark(), 'LanDeck'), ink, tabs);
  const main = h('main', { class: 'main' });
  const movedHost = h('div', { class: 'moved-host' });
  const shell = h('div', { class: 'shell' }, top, movedHost, main, nav);

  const placeInk = () => {
    const cur = tabs.find((t) => t.getAttribute('aria-current') === 'page');
    if (!cur) return;
    const w = Math.min(36, cur.offsetWidth * 0.4);
    ink.style.width = `${w}px`;
    ink.style.transform = `translateX(${cur.offsetLeft + (cur.offsetWidth - w) / 2}px)`;
  };
  new ResizeObserver(placeInk).observe(nav);

  const setTab = (id) => {
    tabs.forEach((t) => (t.dataset.tab === id ? t.setAttribute('aria-current', 'page') : t.removeAttribute('aria-current')));
    placeInk();
  };

  watch('conn', (c) => {
    pill.dataset.state = c;
    pill.querySelector('.pill-text').textContent = CONN_LABEL[c] || c;
    shell.classList.toggle('offline', c !== 'online');
  }, { now: true });
  watch('server', (s) => {
    name.textContent = s?.name || 'LanDeck';
    const where = s && s.name.toLowerCase() !== s.hostname.toLowerCase() ? s.hostname : s?.ips?.[0];
    sub.textContent = s ? [where, `v${s.version}`].filter(Boolean).join(' · ') : 'LanDeck';
  }, { now: true });
  watch('unread', (n) => { badge.textContent = n ? String(Math.min(n, 99)) : ''; }, { now: true });
  watch('moved', (origin) => {
    swap(movedHost, origin && h('div', { class: 'notice warn moved' }, icon('wifi'),
      h('div', { class: 'stack', style: { gap: '8px' } },
        h('div', null, h('b', null, 'PC moved.'), ' This page was opened from an old address. Open the new one so it keeps working, or rescan the QR.'),
        h('div', { class: 'row' },
          h('button', { class: 'btn small primary', onclick: () => { location.href = `${origin}/#handoff=${encodeURIComponent(conn.token)}`; } }, 'Open new address'),
          h('button', { class: 'btn small ghost', onclick: () => set({ moved: null }) }, 'Later')))));
  }, { now: true });

  return { shell, main, setTab };
}

// --- notifications ----------------------------------------------------------

const KIND_ICON = { alert: 'alert', power: 'power', file: 'download', clipboard: 'clipboard', message: 'message', system: 'info' };

function pushNotification(item) {
  const items = [item, ...state.notifications.filter((n) => n.id !== item.id)].slice(0, 100);
  set({ notifications: items, unread: state.unread + 1 });
}

function openNotifications() {
  set({ unread: 0 });
  const list = h('div', { class: 'stack' });
  const render = () => {
    if (!state.notifications.length) {
      swap(list, h('div', { class: 'empty' }, h('div', { class: 'empty-icon' }, icon('bell')), h('p', null, 'Nothing yet. Alerts, uploads and PC events show up here.')));
      return;
    }
    swap(list, h('div', { class: 'list stagger' }, state.notifications.map((n) => h('div', { class: 'item' },
      h('div', { class: `item-icon ${n.level === 'warn' ? 'warn-bg' : 'accent'}` }, icon(KIND_ICON[n.kind] || 'info')),
      h('div', { class: 'item-body' }, h('div', { class: 'item-title' }, n.title), n.body && h('div', { class: 'item-sub', style: { whiteSpace: 'normal' } }, n.body)),
      h('div', { class: 'item-end tiny' }, fmtAgo(n.ts))))));
  };
  render();
  const clearBtn = h('button', { class: 'btn small ghost', onclick: async () => {
    set({ notifications: [] });
    render();
    api.del('/notifications').catch(() => {});
  } }, icon('trash'), 'Clear');
  sheet({ title: 'Notifications', body: list, foot: state.notifications.length ? h('div', { class: 'row' }, h('div', { class: 'spacer' }), clearBtn) : null });
}

async function loadNotifications() {
  try {
    const { items } = await api.get('/notifications');
    const known = new Set(state.notifications.map((n) => n.id));
    const merged = [...state.notifications, ...items.filter((i) => !known.has(i.id))].sort((a, b) => b.ts - a.ts).slice(0, 100);
    set({ notifications: merged });
  } catch { /* fine, they'll stream in */ }
}

// --- PC-pushed events ---------------------------------------------------------

function wireEvents() {
  conn.on('notify', (n) => {
    pushNotification(n);
    if (n.kind === 'alert') toast(n.title, { text: n.body, kind: 'warn', timeout: 6000 });
    else if (n.kind === 'file' || n.kind === 'system') toast(n.title, { text: n.body, kind: 'ok' });
  });
  conn.on('power', (p) => {
    if (p.state === 'going_offline') {
      toast('PC is going offline…', { text: `LanDeck will reconnect when it’s back (${p.action}).`, ico: 'power', timeout: 6000 });
    }
  });
  conn.on('clipboard', ({ item, origin }) => {
    if (origin !== 'pc' || !item) return;
    const what = item.kind === 'image' ? 'an image' : item.secret ? 'something secret' : `“${(item.preview || '').slice(0, 60)}”`;
    toast('PC clipboard', {
      text: `Copied ${what}`, ico: 'clipboard', timeout: 5000,
      action: item.kind === 'text' ? { label: 'Copy', run: () => copyFromPc(item.id) } : null,
    });
  });
  conn.on('features', (f) => set({ features: f }));
  conn.on('revoked', () => {});
  conn.on('online', ({ reconnect }) => {
    flushOutbox();
    loadNotifications();
    if (reconnect) router.refreshActive({ reconnect: true });
  });
  conn.on('unauthorized', () => {
    const app = document.getElementById('app');
    swap(app);
    showPair(app, { revoked: true, onPaired: boot });
  });
}

// --- boot -------------------------------------------------------------------

let shellBuilt = false;

function boot() {
  const app = document.getElementById('app');
  if (location.pathname !== '/') history.replaceState(null, '', `/${location.hash && location.hash !== '#' ? location.hash : '#/home'}`);
  swap(app);
  const { shell, main, setTab } = buildShell();
  app.append(shell);
  if (!shellBuilt) {
    wireEvents();
    shellBuilt = true;
  }
  router.start(main, (active) => setTab(active.tab));
  conn.start();
}

function init() {
  // token handed over from an old address (see "PC moved")
  const hand = location.hash.match(/handoff=([^&]+)/);
  if (hand) {
    conn.setToken(decodeURIComponent(hand[1]));
    history.replaceState(null, '', '/#/home');
  }
  const app = document.getElementById('app');
  const pairing = location.pathname === '/pair';
  if (conn.token && !pairing) {
    boot();
  } else if (!prefs.onboarded && !pairing) {
    showOnboarding(app, () => showPair(app, { onPaired: boot }));
  } else {
    showPair(app, { onPaired: boot, secret: pairing ? location.hash.slice(1) : '' });
  }
  registerServiceWorker();
}

function registerServiceWorker() {
  // Safari refuses service workers on untrusted certificates; the app works without one
  if (!('serviceWorker' in navigator)) return;
  try {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  } catch { /* ignore */ }
}

init();
