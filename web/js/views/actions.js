import { api } from '../api.js';
import { has } from '../icons.js';
import { state } from '../store.js';
import { disabledCard, empty, errorToast, fmtAgo, h, holdButton, icon, sheet, skeletonBlock, swap, tap } from '../ui.js';
import { conn } from '../ws.js';

function resultSheet(res) {
  const ok = res.exit_code === 0 || (res.exit_code == null && !res.error);
  sheet({
    title: res.label,
    body: h('div', { class: 'stack' },
      h('div', { class: 'row' },
        h('div', { class: `item-icon ${ok ? 'accent' : ''}` }, icon(ok ? 'check' : 'alert')),
        h('div', null,
          h('div', { class: 'h3' }, ok ? 'Done' : 'Something went wrong'),
          h('div', { class: 'small faint num' }, res.exit_code != null ? `exit code ${res.exit_code}` : (res.error || 'started'),
            res.duration ? ` · ${res.duration}s` : '', ` · ${fmtAgo(res.ts)}`))),
      res.output && h('pre', { class: 'output' }, res.output)),
  });
}

export default function actions(root) {
  if (state.features.actions === false) {
    root.append(disabledCard('actions', 'Quick actions'));
    return {};
  }
  const grid = h('div', { class: 'action-grid' });
  root.append(grid);
  let last = {};

  const run = async (a, btn, confirm = false) => {
    btn.classList.add('running');
    try {
      const res = await api.post(`/actions/${encodeURIComponent(a.id)}/run`, confirm ? { confirm: true } : {});
      last[a.id] = res;
      const ok = res.exit_code === 0 || (res.exit_code == null && !res.error);
      btn.classList.add(ok ? 'flash-ok' : 'flash-err');
      setTimeout(() => btn.classList.remove('flash-ok', 'flash-err'), 900);
      if (!ok || (res.output && a.type !== 'keys' && a.type !== 'builtin' && a.type !== 'power')) resultSheet(res);
      paintLast(btn, res);
    } catch (err) {
      errorToast(err);
    } finally {
      btn.classList.remove('running');
    }
  };

  const paintLast = (btn, res) => {
    const el = btn.querySelector('.action-last');
    if (!el || !res) return;
    el.textContent = res.exit_code != null && res.exit_code !== 0 ? `exit ${res.exit_code}` : fmtAgo(res.ts);
    el.onclick = (e) => { e.stopPropagation(); resultSheet(res); };
  };

  function tile(a) {
    const ico = has(a.icon) ? a.icon : 'zap';
    const body = [h('div', { class: 'action-icon' }, icon(ico)), h('div', { class: 'action-label' }, a.label), h('div', { class: 'action-last tiny' })];
    if (a.confirm) {
      const btn = holdButton({ label: a.label, kind: 'danger', className: `action c-${a.color || 'cyan'}`, onConfirm: () => run(a, btn, true) });
      swap(btn, h('i', { class: 'hold-fill' }), ...body, h('div', { class: 'tiny faint hold-hint' }, 'hold'));
      return btn;
    }
    const btn = h('button', { class: `action c-${a.color || 'cyan'}`, onclick: () => { tap(btn); run(a, btn); } }, body);
    return btn;
  }

  async function load() {
    if (!grid.children.length) swap(grid, Array.from({ length: 6 }, () => skeletonBlock(112, 20)));
    try {
      const res = await api.get('/actions');
      last = { ...res.last, ...last };
      if (!res.items.length) {
        swap(grid, h('div', { class: 'card span-all' }, empty({ ico: 'zap', text: 'No quick actions yet.', sub: 'Add them in the admin page on the PC.' })));
        return;
      }
      swap(grid, res.items.map((a) => {
        const t = tile(a);
        paintLast(t, last[a.id]);
        return t;
      }));
      grid.classList.add('stagger');
    } catch (err) {
      swap(grid, h('div', { class: 'card span-all' }, empty({ ico: 'wifi-off', text: err.detail || 'Couldn’t load actions.' })));
    }
  }

  const off = conn.on('actions', load);
  load();
  root.append(h('p', { class: 'small faint', style: { margin: '14px 4px' } }, 'Actions are defined on the PC (admin page → Quick actions). The phone can only run them.'));
  return { refresh: load, destroy: off };
}
