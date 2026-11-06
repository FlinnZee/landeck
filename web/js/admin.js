// Admin page. Only reachable from the PC itself (loopback), so it talks to /admin/api directly.
import { brandMark, icon } from './icons.js';
import { fmtAgo, fmtDate, h, sheet, swap, toast, toggle } from './ui.js';

const FEATURE_INFO = {
  dashboard: ['Dashboard', 'Live CPU, memory, disk, network and GPU stats.'],
  power: ['Power', 'Shut down, restart, sleep, lock and schedules.'],
  clipboard: ['Clipboard', 'Two-way clipboard sync and history.'],
  files: ['Files', 'Browse, upload and download inside the shared folders.'],
  media: ['Media & volume', 'Volume, per-app mixer, media keys, output device.'],
  input: ['Remote input', 'Trackpad and keyboard. Works in elevated windows too.'],
  screen: ['Screen', 'Screenshots and live view of the desktop.'],
  processes: ['Processes & apps', 'Process list, ending tasks, launching apps, opening links.'],
  actions: ['Quick actions', 'Run the buttons defined below.'],
  notify: ['Messages', 'Phones can pop up notifications on this PC.'],
  ram: ['RAM cleaner', 'One-tap free up of memory from the phone.'],
  stream: ['Video streaming', 'Watch movies on the phone (HEVC/HDR streamed with ffmpeg).'],
  terminal: ['Terminal', 'Admin PowerShell from the phone. Needs a PIN. Off by default.'],
  remote_action_editing: ['Edit actions from phone', 'Lets paired phones change the quick actions. Off by default.'],
};
const DANGEROUS = new Set(['terminal', 'remote_action_editing']);
const ACTION_TYPES = ['run', 'open_url', 'open_path', 'keys', 'power', 'script', 'builtin'];
const ICONS = ['zap', 'lock', 'monitor-off', 'volume', 'volume-1', 'volume-x', 'play', 'layout', 'activity', 'camera', 'download',
  'refresh', 'moon', 'sun', 'power', 'restart', 'terminal', 'globe', 'folder', 'apps', 'music', 'code', 'shield', 'wifi', 'clock'];
const COLORS = ['cyan', 'violet', 'green', 'rose', 'amber', 'slate'];

async function call(path, { method = 'GET', body } = {}) {
  const res = await fetch(`/admin/api${path}`, {
    method,
    headers: { 'X-LanDeck': '1', ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}) },
    body: body !== undefined ? JSON.stringify(body) : undefined,
    cache: 'no-store',
  });
  const type = res.headers.get('content-type') || '';
  const data = type.includes('json') ? await res.json().catch(() => ({})) : res;
  if (!res.ok) throw Object.assign(new Error(data.detail || res.statusText), data, { status: res.status });
  return data;
}

const fail = (err) => toast('That didn\u2019t work', { text: err.detail || err.message, kind: 'error', timeout: 5000 });

const SECTIONS = [
  ['overview', 'Overview', 'home'],
  ['pair', 'Pair a device', 'scan'],
  ['devices', 'Devices', 'phone'],
  ['features', 'Features', 'sliders'],
  ['folders', 'Folders', 'folder'],
  ['actions', 'Quick actions', 'zap'],
  ['terminal', 'Terminal PIN', 'terminal'],
  ['audit', 'Audit log', 'list'],
  ['settings', 'Settings', 'settings'],
];

let ov = null;
const root = document.getElementById('admin');
const nav = h('nav', { class: 'a-nav' }, h('div', { class: 'a-brand' }, brandMark(), h('div', null, h('b', null, 'LanDeck'), h('div', { class: 'tiny faint' }, 'admin \u00b7 this PC only'))),
  SECTIONS.map(([id, label, ico]) => h('a', { href: `#${id}`, class: 'a-link', dataset: { id } }, icon(ico), label)));
const main = h('main', { class: 'a-main' });
root.append(h('div', { class: 'a-shell' }, nav, main));

const panes = {};
for (const [id, label] of SECTIONS) {
  panes[id] = h('section', { class: 'a-pane', id: `pane-${id}`, 'aria-label': label });
  main.append(panes[id]);
}

function show(id) {
  if (!panes[id]) id = 'overview';
  for (const [k, el] of Object.entries(panes)) el.hidden = k !== id;
  nav.querySelectorAll('.a-link').forEach((a) => a.toggleAttribute('aria-current', a.dataset.id === id));
  renders[id]?.();
}
window.addEventListener('hashchange', () => show(location.hash.slice(1)));

const title = (text, sub, extra) => h('header', { class: 'a-head' }, h('div', null, h('h1', { class: 'h1' }, text), sub && h('p', { class: 'dim' }, sub)), extra);

// --- overview -----------------------------------------------------------------

async function renderOverview() {
  const el = panes.overview;
  const s = ov.server;
  let startup = null;
  const startupBox = h('div', { class: 'card' }, h('div', { class: 'skel', style: { height: '60px' } }));
  const cards = h('div', { class: 'a-grid' },
    stat('PC', s.name, s.hostname), stat('Agent', `v${s.version}`, ov.elevated ? 'running elevated' : 'NOT elevated'),
    stat('Address', (s.ips || []).join(', ') || 'no LAN address', `port ${s.port}`),
    stat('Devices', `${ov.devices.length} paired`, `${ov.devices.filter((d) => d.online).length} online now`));
  swap(el,
    title('Overview', 'Everything LanDeck is doing on this PC.'),
    ov.dry_run && h('div', { class: 'notice warn' }, icon('alert'), 'Dry-run mode: power, input and kill actions are simulated.'),
    !ov.elevated && h('div', { class: 'notice warn' }, icon('shield'), 'LanDeck isn\u2019t running as administrator, so input into elevated windows and startup changes won\u2019t work. Start LANDECK.exe normally (it asks for admin once).'),
    cards,
    h('h2', { class: 'h2 a-sub' }, 'Start with Windows'), startupBox,
    h('h2', { class: 'h2 a-sub' }, 'Certificate'),
    h('div', { class: 'card stack' },
      h('div', { class: 'kv' },
        h('div', { class: 'kv-row' }, h('span', { class: 'faint' }, 'Server SHA-256'), h('code', { class: 'fp' }, ov.cert.fingerprint || '–')),
        h('div', { class: 'kv-row' }, h('span', { class: 'faint' }, 'Local CA SHA-256'), h('code', { class: 'fp' }, ov.cert.ca_fingerprint || '–')),
        h('div', { class: 'kv-row' }, h('span', { class: 'faint' }, 'Valid for'), h('span', { class: 'small' }, [...ov.cert.names, ...ov.cert.ips].join(', ')))),
      h('p', { class: 'small dim' }, 'Compare the fingerprint with what your phone shows under Show Details if you want to be sure you\u2019re talking to this PC. Installing the CA profile on the phone removes the warning; it is limited to .local names and private IP ranges.'),
      h('div', { class: 'row' },
        h('a', { class: 'btn small', href: '/ca.cer', download: 'LanDeck-CA.cer' }, icon('download'), 'CA certificate (.cer)'),
        h('a', { class: 'btn small', href: '/ca.mobileconfig' }, icon('phone'), 'iPhone profile'),
        h('button', { class: 'btn small', onclick: trustCert }, icon('shield'), 'Trust on this PC'))),
    h('h2', { class: 'h2 a-sub' }, 'Video streaming'),
    h('div', { class: `notice ${ov.ffmpeg && ov.ffmpeg.available ? '' : 'warn'}` }, icon(ov.ffmpeg && ov.ffmpeg.available ? 'check' : 'alert'),
      ov.ffmpeg && ov.ffmpeg.available
        ? h('div', null, h('b', null, 'ffmpeg found'), ov.ffmpeg.nvenc ? ' · NVIDIA GPU encoding available' : ' · CPU encoding (no NVENC)',
          h('div', { class: 'small dim mono' }, ov.ffmpeg.path))
        : h('div', null, h('b', null, 'ffmpeg not found.'), ' Movies can’t be streamed to phones until you install it (e.g. ', h('code', null, 'winget install Gyan.FFmpeg'), ') or set its path in the config.')),
    h('h2', { class: 'h2 a-sub' }, 'Maintenance'),
    h('div', { class: 'card row wrap-row' },
      h('button', { class: 'btn', onclick: restart }, icon('restart'), 'Restart agent'),
      h('a', { class: 'btn', href: '/admin/api/diagnostics' }, icon('archive'), 'Export diagnostics (no tokens)'),
      h('span', { class: 'small faint' }, `Data folder: ${ov.data_dir}`)));

  try {
    startup = await call('/startup');
  } catch { startup = { task: 'unknown', networks: [] }; }
  const pub = (startup.networks || []).filter((n) => n.category === 'Public');
  const t = toggle(ov.settings.autostart && startup.task !== 'missing', async (v, undo) => {
    try {
      const r = await call('/startup', { method: 'POST', body: { enabled: v } });
      toast(v ? 'Starts with Windows' : 'Won\u2019t start with Windows', { kind: 'ok', text: `Task: ${r.task}` });
      renderOverview();
    } catch (err) { undo(); fail(err); }
  }, 'Start with Windows');
  swap(startupBox,
    h('div', { class: 'row' }, h('div', { style: { flex: 1 } },
      h('b', null, 'Start at sign-in, elevated, without a UAC prompt'),
      h('div', { class: 'small dim' }, `Task Scheduler task \u201cLanDeck\u201d: ${startup.task}. Also opens TCP ${s.port} for private networks in Windows Firewall.`)), t),
    pub.length && h('div', { class: 'notice warn', style: { marginTop: '12px' } }, icon('wifi'),
      h('div', null, h('b', null, `${pub.map((n) => n.interface).join(', ')} is set to Public.`), ' Windows blocks phones on Public networks. Switch it to Private in Settings \u2192 Network & internet \u2192 your network \u2192 Network profile type.')));
}

function stat(label, value, sub) {
  return h('div', { class: 'card a-stat' }, h('div', { class: 'eyebrow' }, label), h('div', { class: 'a-stat-v ellipsis' }, value), h('div', { class: 'small faint ellipsis' }, sub));
}

async function trustCert() {
  try {
    await call('/trust-cert', { method: 'POST' });
    toast('Certificate trusted on this PC', { kind: 'ok', text: 'Reload this page to drop the warning.' });
  } catch (err) { fail(err); }
}

async function restart() {
  try {
    await call('/restart', { method: 'POST' });
    toast('Restarting\u2026', { text: 'This page reconnects in a few seconds.' });
    setTimeout(waitBack, 2500);
  } catch (err) { fail(err); }
}

async function waitBack() {
  for (let i = 0; i < 40; i++) {
    try { await call('/overview'); location.reload(); return; } catch { await new Promise((r) => setTimeout(r, 1000)); }
  }
}

// --- pairing ------------------------------------------------------------------

let pairTimer = null;

function renderPair() {
  const el = panes.pair;
  const box = h('div', { class: 'pair-box' });
  swap(el, title('Pair a device', 'Scan the code with the phone camera. It\u2019s valid for 2 minutes and works once.'), box);
  idle();

  function idle(msg) {
    clearInterval(pairTimer);
    swap(box, h('div', { class: 'card pair-idle' },
      h('div', { class: 'onb-icon' }, icon('scan')),
      msg && h('p', { class: 'dim' }, msg),
      h('button', { class: 'btn primary', onclick: start }, icon('scan'), 'Show pairing code')));
  }

  async function start() {
    let p;
    try { p = await call('/pair', { method: 'POST' }); } catch (err) { fail(err); return; }
    // default to the landeck.local QR: it connects far more reliably from iPhones than a raw IP
    let mdns = true;
    const qr = h('div', { class: 'qr', html: p.qr_mdns });
    const link = h('code', { class: 'small pair-url' }, p.urls.mdns);
    const left = h('span', { class: 'num' });
    const ring = h('div', { class: 'pair-ring' });
    const swapQr = h('button', { class: 'btn small ghost', onclick: () => {
      mdns = !mdns;
      qr.innerHTML = mdns ? p.qr_mdns : p.qr;
      link.textContent = mdns ? p.urls.mdns : p.urls.ip;
      swapQr.lastChild.textContent = mdns ? 'Use IP address' : 'Use landeck.local';
    } }, icon('globe'), h('span', null, 'Use IP address'));
    swap(box, h('div', { class: 'card pair-live' },
      qr,
      h('div', { class: 'pair-side stack' },
        h('div', { class: 'eyebrow' }, 'Or type this code on the phone'),
        h('div', { class: 'pair-code num' }, `${p.code.slice(0, 3)} ${p.code.slice(3)}`),
        h('div', { class: 'small dim' }, 'at ', h('b', null, p.urls.host)),
        h('div', { class: 'row small dim' }, ring, h('span', null, 'Expires in ', left)),
        link,
        h('div', { class: 'row wrap-row' }, swapQr, h('button', { class: 'btn small ghost', onclick: async () => { await call('/pair', { method: 'DELETE' }).catch(() => {}); idle(); } }, icon('x'), 'Cancel')),
        h('div', { class: 'notice small' }, icon('info'), 'When the phone asks, a Yes/No box pops up on this PC. Only say Yes if you just scanned it.'))));
    const until = p.expires * 1000;
    const tick = () => {
      const s = Math.max(0, Math.round((until - Date.now()) / 1000));
      left.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
      ring.style.setProperty('--p', String(s / p.ttl));
      if (s <= 0) idle('That code expired.');
    };
    tick();
    pairTimer = setInterval(async () => {
      tick();
      // stop once someone paired
      if (ov && !document.hidden) {
        const before = ov.devices.length;
        await refreshOverview();
        if (ov.devices.length > before) {
          idle(`Paired: ${ov.devices[ov.devices.length - 1].name}`);
          toast('Device paired', { kind: 'ok', text: ov.devices[ov.devices.length - 1].name });
        }
      }
    }, 1000);
  }
}

// --- devices -----------------------------------------------------------------

function renderDevices() {
  const el = panes.devices;
  const rows = ov.devices.map((d) => h('tr', null,
    h('td', null, h('div', { class: 'row', style: { gap: '10px' } }, h('i', { class: `dot-sm ${d.online ? 'on' : ''}`, title: d.online ? 'Online' : 'Offline' }), h('b', null, d.name))),
    h('td', { class: 'dim' }, d.online ? 'online now' : fmtAgo(d.last_seen)),
    h('td', { class: 'dim num' }, d.last_ip || '–'),
    h('td', { class: 'dim' }, fmtDate(d.created)),
    h('td', { class: 'right' },
      h('button', { class: 'btn small ghost', onclick: () => renameDevice(d) }, icon('pencil'), 'Rename'),
      h('button', { class: 'btn small danger', onclick: () => revoke(d) }, icon('ban'), 'Revoke'))));
  swap(el, title('Devices', 'Phones that can control this PC. Revoking cuts them off immediately.',
    h('a', { class: 'btn primary', href: '#pair' }, icon('plus'), 'Pair new')),
  ov.devices.length
    ? h('div', { class: 'card flush' }, h('table', { class: 'a-table' },
      h('thead', null, h('tr', null, ['Device', 'Last seen', 'Last address', 'Paired', ''].map((t) => h('th', null, t)))), h('tbody', null, rows)))
    : h('div', { class: 'card' }, h('div', { class: 'empty' }, h('div', { class: 'empty-icon' }, icon('phone')), h('p', null, 'No devices yet.'), h('a', { class: 'btn primary small', href: '#pair' }, 'Pair one'))));
}

async function revoke(d) {
  const s = sheet({ title: `Revoke ${d.name}?`, body: h('div', { class: 'stack' },
    h('p', { class: 'dim' }, 'Its token is deleted and any open connection is closed right away. Pair it again to come back.'),
    h('button', { class: 'btn danger', onclick: async () => {
      try {
        await call(`/devices/${d.id}`, { method: 'DELETE' });
        s.close();
        toast('Revoked', { kind: 'ok', text: d.name });
        await refreshOverview();
        renderDevices();
      } catch (err) { fail(err); }
    } }, icon('ban'), 'Revoke')) });
}

function renameDevice(d) {
  const input = h('input', { class: 'input', value: d.name, maxlength: 48, autofocus: true });
  const s = sheet({ title: 'Rename device', body: h('div', { class: 'stack' }, input, h('button', { class: 'btn primary', onclick: async () => {
    try {
      await call(`/devices/${d.id}`, { method: 'PATCH', body: { name: input.value } });
      s.close();
      await refreshOverview();
      renderDevices();
    } catch (err) { fail(err); }
  } }, 'Save')) });
}

// --- features ------------------------------------------------------------------

function renderFeatures() {
  const el = panes.features;
  swap(el, title('Features', 'Switch whole areas on or off. Phones see the change instantly.'),
    h('div', { class: 'a-features' }, ov.feature_names.map((name) => {
      const [label, desc] = FEATURE_INFO[name] || [name, ''];
      const t = toggle(ov.features[name], async (v, undo) => {
        try {
          const r = await call('/features', { method: 'PUT', body: { [name]: v } });
          ov.features = r.features;
          if (name === 'terminal' && v && !ov.pin_set) toast('Set a PIN next', { text: 'The terminal stays locked until a PIN exists.', kind: 'warn', action: { label: 'Set PIN', run: () => { location.hash = 'terminal'; } } });
        } catch (err) { undo(); fail(err); }
      }, label);
      return h('div', { class: `card feature ${DANGEROUS.has(name) ? 'danger-edge' : ''}` },
        h('div', { style: { flex: 1 } }, h('div', { class: 'h3' }, label, DANGEROUS.has(name) && h('span', { class: 'tag danger' }, 'sensitive')), h('div', { class: 'small dim' }, desc)), t);
    })));
}

// --- folders ------------------------------------------------------------------

function renderFolders() {
  const el = panes.folders;
  const path = h('input', { class: 'input', placeholder: 'D:\\Media', 'aria-label': 'Folder path' });
  const name = h('input', { class: 'input', placeholder: 'Name (optional)', 'aria-label': 'Folder name' });
  const upload = h('input', { class: 'input', value: ov.settings.upload_dir || '', placeholder: ov.upload_dir_effective, 'aria-label': 'Upload folder' });
  const defaultOn = ov.settings.default_roots !== false;
  const drivesOn = ov.settings.show_drives !== false;
  const tagFor = (r) => (r.drive ? h('span', { class: 'tag' }, 'drive') : h('span', { class: 'tag' }, 'default'));
  swap(el, title('Folders', 'Phones can only see inside these folders and drives. Paths are checked so links and \u201c..\u201d can\u2019t escape.'),
    h('div', { class: 'card flush' }, h('table', { class: 'a-table' },
      h('thead', null, h('tr', null, h('th', null, 'Name'), h('th', null, 'Path'), h('th', null, ''))),
      h('tbody', null, ov.roots.map((r) => h('tr', null, h('td', null, h('b', null, r.name)), h('td', { class: 'dim mono small' }, r.path),
        h('td', { class: 'right' }, r.builtin ? tagFor(r) : h('button', { class: 'btn small ghost', onclick: () => removeRoot(r.path) }, icon('trash'), 'Remove'))))))),
    h('div', { class: 'card row wrap-row', style: { marginTop: '12px' } },
      h('div', { style: { flex: 1 } }, h('b', null, 'All drives'), h('div', { class: 'small dim' }, 'Every drive connected right now \u2014 C:, other disks, USB sticks, SD cards, network drives.')),
      toggle(drivesOn, async (v, undo) => { try { await call('/show-drives', { method: 'PUT', body: { enabled: v } }); await refreshOverview(); renderFolders(); } catch (err) { undo(); fail(err); } }, 'Show all drives')),
    h('div', { class: 'card row wrap-row' },
      h('div', { style: { flex: 1 } }, h('b', null, 'Default folders'), h('div', { class: 'small dim' }, 'Downloads, Desktop, Documents, Pictures and Videos.')),
      toggle(defaultOn, async (v, undo) => { try { await call('/default-roots', { method: 'PUT', body: { enabled: v } }); await refreshOverview(); renderFolders(); } catch (err) { undo(); fail(err); } }, 'Default folders')),
    h('h2', { class: 'h2 a-sub' }, 'Add a folder'),
    h('div', { class: 'card row wrap-row' }, h('div', { style: { flex: 2, minWidth: '240px' } }, path), h('div', { style: { flex: 1, minWidth: '160px' } }, name),
      h('button', { class: 'btn primary', onclick: async () => {
        try {
          await call('/roots', { method: 'POST', body: { path: path.value, name: name.value } });
          toast('Folder shared', { kind: 'ok' });
          await refreshOverview();
          renderFolders();
        } catch (err) { fail(err); }
      } }, icon('plus'), 'Add')),
    h('h2', { class: 'h2 a-sub' }, 'Where phone uploads land'),
    h('div', { class: 'card stack' }, h('div', { class: 'row' }, upload, h('button', { class: 'btn', onclick: async () => {
      try { await call('/settings', { method: 'PUT', body: { upload_dir: upload.value.trim() } }); toast('Saved', { kind: 'ok' }); await refreshOverview(); renderFolders(); } catch (err) { fail(err); }
    } }, 'Save')), h('p', { class: 'small faint' }, `Empty = ${ov.upload_dir_effective}. Uploads into a browsed folder go there instead.`)));
}

async function removeRoot(path) {
  try { await call('/roots', { method: 'DELETE', body: { path } }); await refreshOverview(); renderFolders(); } catch (err) { fail(err); }
}

// --- actions editor -------------------------------------------------------------------

async function renderActions() {
  const el = panes.actions;
  let data;
  try { data = await call('/actions'); } catch (err) { fail(err); return; }
  let items = data.items.map((a) => ({ ...a }));
  const list = h('div', { class: 'stack' });
  const saveBtn = h('button', { class: 'btn primary', onclick: save }, icon('check'), 'Save actions');

  const field = (label, input) => h('label', { class: 'field' }, h('span', null, label), input);
  const select = (value, options, onChange) => {
    const s = h('select', { class: 'input' }, options.map((o) => h('option', { value: o, selected: o === value }, o)));
    s.addEventListener('change', () => onChange(s.value));
    return s;
  };
  const text = (value, onInput, ph = '') => {
    const i = h('input', { class: 'input', value: value ?? '', placeholder: ph });
    i.addEventListener('input', () => onInput(i.value));
    return i;
  };

  function typeFields(a) {
    switch (a.type) {
      case 'run': return [field('Program', text(a.exe, (v) => { a.exe = v; }, 'notepad.exe')),
        field('Arguments (one per line)', (() => { const t = h('textarea', { class: 'textarea', rows: 2 }); t.value = (a.args || []).join('\n'); t.addEventListener('input', () => { a.args = t.value.split('\n').filter(Boolean); }); return t; })()),
        field('Wait for exit and show output', select(String(a.wait ?? true), ['true', 'false'], (v) => { a.wait = v === 'true'; }))];
      case 'open_url': return [field('URL', text(a.url, (v) => { a.url = v; }, 'https://\u2026 or ms-settings:\u2026'))];
      case 'open_path': return [field('Path', text(a.path, (v) => { a.path = v; }, '%USERPROFILE%\\Downloads'))];
      case 'keys': return [field('Keys (separated by +)', text((a.keys || []).join('+'), (v) => { a.keys = v.split('+').map((k) => k.trim().toLowerCase()).filter(Boolean); }, 'ctrl+shift+esc'))];
      case 'power': return [field('Power action', select(a.action || 'lock', ['lock', 'sleep', 'hibernate', 'restart', 'shutdown', 'signout'], (v) => { a.action = v; }))];
      case 'script': return [field(`Script in ${data.scripts_dir}`, data.scripts.length ? select(a.file || data.scripts[0], data.scripts, (v) => { a.file = v; }) : text(a.file, (v) => { a.file = v; }, 'backup.ps1'))];
      case 'builtin': return [field('Built-in', select(a.name || 'mute_toggle', ['mute_toggle', 'volume', 'screenshot', 'display_off'], (v) => { a.name = v; render(); })),
        a.name === 'volume' && field('Volume %', text(a.value ?? 30, (v) => { a.value = Number(v) || 0; }))];
      default: return [];
    }
  }

  function card(a, i) {
    const body = h('div', { class: 'a-action-body', hidden: !a._open },
      h('div', { class: 'a-form' },
        field('Label', text(a.label, (v) => { a.label = v; head.querySelector('b').textContent = v || '(no label)'; })),
        field('Id', text(a.id, (v) => { a.id = v; }, 'my-action')),
        field('Icon', select(a.icon || 'zap', ICONS, (v) => { a.icon = v; render(); })),
        field('Colour', select(a.color || 'cyan', COLORS, (v) => { a.color = v; })),
        field('Type', select(a.type, ACTION_TYPES, (v) => { a.type = v; render(); })),
        field('Hold to confirm on phone', select(String(!!a.confirm), ['false', 'true'], (v) => { a.confirm = v === 'true'; })),
        ...typeFields(a).filter(Boolean)),
      h('div', { class: 'row' }, h('div', { class: 'spacer' }),
        h('button', { class: 'btn small ghost', disabled: i === 0, onclick: () => { [items[i - 1], items[i]] = [items[i], items[i - 1]]; render(); } }, icon('arrow-up')),
        h('button', { class: 'btn small ghost', disabled: i === items.length - 1, onclick: () => { [items[i + 1], items[i]] = [items[i], items[i + 1]]; render(); } }, icon('arrow-down')),
        h('button', { class: 'btn small danger', onclick: () => { items.splice(i, 1); render(); } }, icon('trash'), 'Delete')));
    const head = h('button', { class: 'a-action-head', onclick: () => { a._open = !a._open; body.hidden = !a._open; } },
      h('div', { class: `action-icon c-${a.color || 'cyan'}` }, icon(ICONS.includes(a.icon) ? a.icon : 'zap')),
      h('b', null, a.label || '(no label)'), h('span', { class: 'tag' }, a.type), a.confirm && h('span', { class: 'tag danger' }, 'hold'),
      h('span', { class: 'spacer' }), icon('chevron-down'));
    return h('div', { class: 'card a-action' }, head, body);
  }

  function render() {
    swap(list, items.map(card));
  }

  async function save() {
    const clean = items.map(({ _open, ...a }) => a);
    try {
      const r = await call('/actions', { method: 'PUT', body: { items: clean } });
      items = r.items.map((a) => ({ ...a }));
      render();
      toast('Actions saved', { kind: 'ok', text: 'Phones refresh automatically.' });
    } catch (err) { fail(err); }
  }

  swap(el, title('Quick actions', 'Buttons on the phone\u2019s Control tab. Phones can only press them, never change what they do.',
    h('div', { class: 'row' }, h('button', { class: 'btn', onclick: () => { items.push({ id: `action-${items.length + 1}`, label: 'New action', icon: 'zap', color: 'cyan', type: 'keys', keys: ['win', 'd'], _open: true }); render(); } }, icon('plus'), 'Add'), saveBtn)),
  h('p', { class: 'small faint', style: { margin: '-6px 0 14px' } }, `Programs run without a shell (arguments are passed as a list). Scripts live in ${data.scripts_dir} and run with -NoProfile -ExecutionPolicy Bypass.`),
  list);
  render();
}

// --- terminal pin ----------------------------------------------------------------

function renderTerminal() {
  const el = panes.terminal;
  const pin = h('input', { class: 'input', type: 'password', inputmode: 'numeric', autocomplete: 'new-password', placeholder: 'At least 6 digits', 'aria-label': 'New PIN' });
  const pin2 = h('input', { class: 'input', type: 'password', inputmode: 'numeric', autocomplete: 'new-password', placeholder: 'Repeat', 'aria-label': 'Repeat PIN' });
  swap(el, title('Terminal PIN', 'The phone terminal runs PowerShell as administrator. Every session needs this PIN, locks after 30 minutes idle, and every command lands in the audit log.'),
    h('div', { class: `notice ${ov.features.terminal ? 'danger' : ''}` }, icon(ov.features.terminal ? 'alert' : 'lock'),
      ov.features.terminal ? 'The terminal feature is ON.' : 'The terminal feature is off. Turn it on under Features after setting a PIN.'),
    h('div', { class: 'card stack', style: { marginTop: '12px', maxWidth: '520px' } },
      h('div', { class: 'h3' }, ov.pin_set ? 'Change PIN' : 'Set a PIN'),
      pin, pin2,
      h('div', { class: 'row' },
        h('button', { class: 'btn primary', onclick: async () => {
          if (pin.value !== pin2.value) { toast('PINs don\u2019t match', { kind: 'error' }); return; }
          try { await call('/pin', { method: 'POST', body: { pin: pin.value } }); toast('PIN saved', { kind: 'ok', text: 'Open terminal sessions were locked.' }); await refreshOverview(); renderTerminal(); } catch (err) { fail(err); }
        } }, icon('check'), 'Save PIN'),
        ov.pin_set && h('button', { class: 'btn danger', onclick: async () => {
          try { await call('/pin', { method: 'DELETE' }); toast('PIN removed', { kind: 'ok' }); await refreshOverview(); renderTerminal(); } catch (err) { fail(err); }
        } }, 'Remove PIN'))));
}

// --- audit ---------------------------------------------------------------------

function renderAudit() {
  const el = panes.audit;
  const q = h('input', { class: 'input', type: 'search', placeholder: 'Filter (device, path, command\u2026)', 'aria-label': 'Filter audit log' });
  const kind = h('select', { class: 'input', 'aria-label': 'Action type' },
    ['', 'pair', 'auth', 'power', 'files', 'clipboard', 'process', 'action', 'terminal', 'screen', 'apps', 'admin'].map((k) => h('option', { value: k }, k || 'All actions')));
  const body = h('tbody');
  const load = async () => {
    try {
      const r = await call(`/audit?limit=800&q=${encodeURIComponent(q.value)}&action=${encodeURIComponent(kind.value)}`);
      swap(body, r.items.length ? r.items.map((e) => h('tr', { class: e.result !== 'ok' ? 'bad' : '' },
        h('td', { class: 'dim num nowrap' }, new Date(e.ts * 1000).toLocaleString()),
        h('td', null, e.device || h('span', { class: 'faint' }, '–')),
        h('td', { class: 'num dim' }, e.ip || ''),
        h('td', null, h('code', null, e.action)),
        h('td', { class: 'mono small wrapcell' }, e.target),
        h('td', { class: e.result === 'ok' ? 'ok' : 'danger' }, e.result))) : h('tr', null, h('td', { colspan: 6, class: 'faint' }, 'Nothing logged yet.')));
    } catch (err) { fail(err); }
  };
  q.addEventListener('input', () => { clearTimeout(q.t); q.t = setTimeout(load, 250); });
  kind.addEventListener('change', load);
  swap(el, title('Audit log', 'Pairings, failed logins, power, file changes, kills, actions and terminal commands. Tokens are never logged.',
    h('button', { class: 'btn', onclick: load }, icon('refresh'), 'Refresh')),
  h('div', { class: 'row', style: { marginBottom: '12px' } }, h('div', { style: { flex: 1 } }, q), h('div', { style: { width: '200px' } }, kind)),
  h('div', { class: 'card flush a-scroll' }, h('table', { class: 'a-table' },
    h('thead', null, h('tr', null, ['When', 'Device', 'IP', 'Action', 'Target', 'Result'].map((t) => h('th', null, t)))), body)));
  load();
}

// --- settings -------------------------------------------------------------------

function renderSettings() {
  const el = panes.settings;
  const name = h('input', { class: 'input', value: ov.settings.name || '', 'aria-label': 'PC name' });
  const port = h('input', { class: 'input', type: 'number', value: ov.settings.port, min: 1024, max: 65535, 'aria-label': 'Port' });
  const putSetting = async (body, msg = 'Saved') => {
    try {
      const r = await call('/settings', { method: 'PUT', body });
      toast(msg, { kind: 'ok', text: r.restart_needed ? 'Restart the agent to apply.' : '' });
      await refreshOverview();
    } catch (err) { fail(err); }
  };
  swap(el, title('Settings'),
    h('div', { class: 'stack', style: { maxWidth: '640px' } },
      h('div', { class: 'card stack' }, h('div', { class: 'h3' }, 'PC name shown on phones'),
        h('div', { class: 'row' }, name, h('button', { class: 'btn', onclick: () => putSetting({ name: name.value.trim() }) }, 'Save'))),
      h('div', { class: 'card row' }, h('div', { style: { flex: 1 } }, h('div', { class: 'h3' }, 'Keep clipboard history on disk'), h('div', { class: 'small dim' }, 'Off: history lives in memory and disappears when the agent stops.')),
        toggle(ov.settings.clipboard_persist, (v) => putSetting({ clipboard_persist: v }), 'Persist clipboard')),
      h('div', { class: 'card row' }, h('div', { style: { flex: 1 } }, h('div', { class: 'h3' }, 'Notify when the screen is viewed'), h('div', { class: 'small dim' }, 'Shows a Windows toast when a phone starts the live view. The tray icon turns red either way.')),
        toggle(ov.settings.screen_toast, (v) => putSetting({ screen_toast: v }), 'Screen toast')),
      h('div', { class: 'card stack' }, h('div', { class: 'h3' }, 'Port'),
        h('div', { class: 'row' }, port, h('button', { class: 'btn', onclick: () => putSetting({ port: Number(port.value) }, 'Port saved') }, 'Save')),
        h('p', { class: 'small faint' }, 'Phones paired with the old port need the new address (or landeck.local) after a restart.'))));
}

// --- boot -----------------------------------------------------------------------

const renders = {
  overview: renderOverview, pair: renderPair, devices: renderDevices, features: renderFeatures, folders: renderFolders,
  actions: renderActions, terminal: renderTerminal, audit: renderAudit, settings: renderSettings,
};

async function refreshOverview() {
  ov = await call('/overview');
  nav.querySelector('[data-id="devices"]')?.setAttribute('data-count', String(ov.devices.length));
  return ov;
}

async function boot() {
  try {
    await refreshOverview();
  } catch (err) {
    swap(main, h('div', { class: 'notice danger' }, icon('alert'), err.detail || 'Can\u2019t reach the agent.'));
    return;
  }
  show(location.hash.slice(1) || 'overview');
  setInterval(async () => {
    if (document.hidden) return;
    const cur = location.hash.slice(1) || 'overview';
    try {
      const before = JSON.stringify(ov.devices.map((d) => [d.id, d.online, Math.round(d.last_seen / 60)]));
      await refreshOverview();
      if (cur === 'devices' && JSON.stringify(ov.devices.map((d) => [d.id, d.online, Math.round(d.last_seen / 60)])) !== before) renderDevices();
    } catch { /* agent restarting */ }
  }, 4000);
}

boot();
