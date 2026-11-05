import { api, url } from '../api.js';
import { back, navigate } from '../router.js';
import { prefs, setPref, state } from '../store.js';
import { debounce, disabledCard, empty, errorToast, fmtBytes, h, holdButton, icon, sheet, skeletonList, swap, toast } from '../ui.js';
import { conn } from '../ws.js';

function header(title, extra) {
  return h('div', { class: 'section-title' },
    h('button', { class: 'icon-btn back', 'aria-label': 'Back', onclick: () => back('/more') }, icon('chevron-left')),
    h('div', { class: 'h2', style: { flex: 1 } }, title), extra);
}

const SORT = {
  cpu: (a, b) => b.cpu - a.cpu || b.mem - a.mem,
  mem: (a, b) => b.mem - a.mem,
  name: (a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }),
  pid: (a, b) => a.pid - b.pid,
};

export default function processes(root) {
  const wrap = h('div', { class: 'wrap' });
  root.append(wrap);
  if (state.features.processes === false) {
    wrap.append(header('Processes'), disabledCard('processes', 'Process control'));
    return {};
  }
  const search = h('input', { class: 'input', type: 'search', placeholder: 'Filter by name or PID', 'aria-label': 'Filter processes' });
  const sortChips = h('div', { class: 'chips' });
  const count = h('span', { class: 'tiny faint num' });
  const list = h('div', { class: 'list proc-list' });
  wrap.append(header('Processes', count), h('div', { class: 'search', style: { marginBottom: '10px' } }, icon('search'), search), sortChips,
    h('div', { style: { height: '12px' } }), list);
  swap(list, skeletonList(10));

  let rows = [];
  let maxMem = 1;
  const paintSort = () => swap(sortChips, [['cpu', 'CPU'], ['mem', 'Memory'], ['name', 'Name'], ['pid', 'PID']].map(([k, label]) =>
    h('button', { class: 'chip', 'aria-pressed': String(prefs.procSort === k), onclick: () => { setPref('procSort', k); paintSort(); render(); } }, label)));
  paintSort();

  function render() {
    const q = search.value.trim().toLowerCase();
    const shown = rows.filter((r) => !q || r.name.toLowerCase().includes(q) || String(r.pid).startsWith(q)).sort(SORT[prefs.procSort] || SORT.cpu);
    count.textContent = `${shown.length} running`;
    if (!shown.length) {
      swap(list, empty({ ico: 'search', text: 'No process matches.' }));
      return;
    }
    swap(list, shown.slice(0, 200).map((p) => h('button', { class: 'item proc', onclick: () => details(p) },
      h('div', { class: 'proc-main' },
        h('div', { class: 'item-title' }, p.name.replace(/\.exe$/i, '') || `PID ${p.pid}`, p.protected && icon('shield', 'tiny-icon')),
        h('div', { class: 'item-sub num' }, `${p.pid}${p.user ? ` · ${p.user}` : ''}`)),
      h('div', { class: 'proc-stats num' },
        h('div', { class: 'proc-stat' }, h('span', null, `${p.cpu.toFixed(1)}%`), h('div', { class: `bar thin ${p.cpu > 50 ? 'warn' : ''}` }, h('i', { style: { width: `${Math.min(100, p.cpu)}%` } }))),
        h('div', { class: 'proc-stat' }, h('span', null, fmtBytes(p.mem)), h('div', { class: 'bar thin' }, h('i', { style: { width: `${(p.mem / maxMem) * 100}%` } })))))));
  }

  function details(p) {
    const kill = (tree) => holdButton({ label: tree ? 'End process tree' : 'End process', ico: 'kill', onConfirm: async () => {
      try {
        const res = await api.post(`/processes/${p.pid}/kill`, { confirm: true, tree });
        toast(`Ended ${res.killed.length} process${res.killed.length > 1 ? 'es' : ''}`, { kind: 'ok', text: res.killed.map((k) => k.name).slice(0, 4).join(', ') });
        s.close();
      } catch (err) { errorToast(err, 'Couldn’t end it'); }
    } });
    const s = sheet({ title: p.name, body: h('div', { class: 'stack' },
      h('div', { class: 'kv' },
        [['PID', p.pid], ['Parent', p.ppid ?? '–'], ['User', p.user || '–'], ['CPU', `${p.cpu.toFixed(1)}%`], ['Memory', fmtBytes(p.mem)]]
          .map(([k, v]) => h('div', { class: 'kv-row' }, h('span', { class: 'faint' }, k), h('span', { class: 'num' }, String(v))))),
      p.protected
        ? h('div', { class: 'notice' }, icon('shield'), 'Protected: Windows (or LanDeck itself) needs this process, so it can’t be ended from here.')
        : h('div', { class: 'grid-2' }, kill(false), kill(true))) });
  }

  const onData = (d) => {
    rows = d.items;
    maxMem = Math.max(1, ...rows.map((r) => r.mem));
    render();
  };
  const off = conn.on('processes', onData);
  const unsub = conn.sub('processes');
  search.addEventListener('input', debounce(render, 120));
  api.get('/processes').then(onData).catch((err) => swap(list, empty({ ico: 'alert', text: err.detail || 'Couldn’t load processes.' })));
  return { destroy() { off(); unsub(); } };
}

export function appsView(root) {
  const wrap = h('div', { class: 'wrap' });
  root.append(wrap);
  if (state.features.processes === false) {
    wrap.append(header('Apps'), disabledCard('processes', 'The app launcher'));
    return {};
  }
  const urlInput = h('input', { class: 'input', type: 'url', inputmode: 'url', placeholder: 'example.com', autocapitalize: 'off', autocomplete: 'off', 'aria-label': 'Link to open on the PC' });
  const openBtn = h('button', { class: 'btn primary', onclick: openUrl }, icon('external'), 'Open');
  urlInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') openUrl(); });
  const search = h('input', { class: 'input', type: 'search', placeholder: 'Search apps', 'aria-label': 'Search apps' });
  const grid = h('div', { class: 'app-grid' });
  wrap.append(header('Apps', h('button', { class: 'btn small ghost', onclick: () => navigate('/remote/keyboard') }, icon('keyboard'), 'Type into PC')),
    h('div', { class: 'card stack' }, h('div', { class: 'h3' }, 'Open a link on the PC'), h('div', { class: 'row' }, urlInput, openBtn)),
    h('div', { class: 'search', style: { margin: '16px 0 12px' } }, icon('search'), search),
    grid);
  swap(grid, Array.from({ length: 12 }, () => h('div', { class: 'app skel-app' }, h('div', { class: 'skel', style: { width: '52px', height: '52px', borderRadius: '14px' } }), h('div', { class: 'skel', style: { width: '60px', height: '10px' } }))));

  let apps = [];

  async function openUrl() {
    const v = urlInput.value.trim();
    if (!v) { urlInput.focus(); return; }
    try {
      const res = await api.post('/apps/open-url', { url: v });
      toast('Opened on the PC', { kind: 'ok', text: res.url });
      urlInput.value = '';
    } catch (err) { errorToast(err); }
  }

  async function launch(a, btn) {
    btn.classList.add('launching');
    try {
      await api.post(`/apps/${a.id}/launch`);
      toast(`Opening ${a.name}`, { kind: 'ok', timeout: 1800 });
    } catch (err) { errorToast(err); } finally {
      setTimeout(() => btn.classList.remove('launching'), 600);
    }
  }

  function render() {
    const q = search.value.trim().toLowerCase();
    const shown = apps.filter((a) => !q || a.name.toLowerCase().includes(q) || a.group.toLowerCase().includes(q));
    if (!shown.length) {
      swap(grid, h('div', { class: 'span-all' }, empty({ ico: 'apps', text: q ? 'No app matches.' : 'No Start Menu shortcuts found.' })));
      return;
    }
    swap(grid, shown.map((a) => {
      const btn = h('button', { class: 'app', onclick: () => launch(a, btn), title: a.name },
        h('div', { class: 'app-icon' }, h('img', { src: url(a.icon), alt: '', loading: 'lazy', decoding: 'async', onerror: (e) => e.target.replaceWith(icon('apps')) })),
        h('div', { class: 'app-name' }, a.name));
      return btn;
    }));
  }

  async function load(refresh = false) {
    try {
      ({ items: apps } = await api.get(`/apps${refresh ? '?refresh=1' : ''}`));
      render();
    } catch (err) {
      swap(grid, h('div', { class: 'span-all' }, empty({ ico: 'wifi-off', text: err.detail || 'Couldn’t load apps.' })));
    }
  }

  search.addEventListener('input', debounce(render, 100));
  load();
  return { refresh: () => load() };
}
