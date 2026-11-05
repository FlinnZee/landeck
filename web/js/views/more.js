import { api } from '../api.js';
import { navigate } from '../router.js';
import { state, watch } from '../store.js';
import { errorToast, h, icon, sheet, swap, toast, toggle } from '../ui.js';

export function messageSheet() {
  const title = h('input', { class: 'input', placeholder: 'Title (optional)', maxlength: 120 });
  const body = h('textarea', { class: 'textarea', placeholder: 'Message for whoever is at the PC', maxlength: 1000, autofocus: true });
  let sound = true;
  const send = h('button', { class: 'btn primary block', onclick: async () => {
    if (!title.value.trim() && !body.value.trim()) { body.focus(); return; }
    send.disabled = true;
    try {
      await api.post('/notify', { title: title.value.trim(), body: body.value.trim(), sound });
      toast('Shown on the PC', { kind: 'ok' });
      s.close();
    } catch (err) {
      send.disabled = false;
      errorToast(err);
    }
  } }, icon('send'), 'Send to PC');
  const s = sheet({ title: 'Message to PC', body: h('div', { class: 'stack' }, title, body,
    h('label', { class: 'row', style: { justifyContent: 'space-between' } }, h('span', { class: 'dim' }, 'Play a sound'), toggle(true, (v) => { sound = v; }, 'Play a sound')),
    send) });
}

export default function more(root) {
  const wrap = h('div', { class: 'wrap' });
  root.append(wrap);
  const f = () => state.features;

  const entry = (ico, title, sub, onClick, { feature, end } = {}) => {
    const off = feature && f()[feature] === false;
    return h('button', { class: `item${off ? ' is-off' : ''}`, onclick: onClick },
      h('div', { class: 'item-icon accent' }, icon(ico)),
      h('div', { class: 'item-body' }, h('div', { class: 'item-title' }, title), h('div', { class: 'item-sub' }, off ? 'Turned off on the PC' : sub)),
      h('div', { class: 'item-end' }, end || icon('chevron-right')));
  };

  const render = () => swap(wrap,
    h('div', { class: 'section-title' }, h('div', { class: 'h2' }, 'More')),
    h('div', { class: 'list stagger' },
      entry('clipboard', 'Clipboard', 'Sync, history and pinned clips', () => navigate('/clipboard'), { feature: 'clipboard' }),
      entry('activity', 'Processes', 'What’s running, end tasks', () => navigate('/processes'), { feature: 'processes' }),
      entry('apps', 'Apps', 'Launch from the Start Menu, open links', () => navigate('/apps'), { feature: 'processes' }),
      entry('message', 'Message to PC', 'Pop up a notification on the PC', messageSheet, { feature: 'notify' }),
      entry('terminal', 'Terminal', f().terminal ? 'PowerShell, PIN protected' : 'Off by default', () => navigate('/terminal'),
        { end: f().terminal ? icon('chevron-right') : icon('lock') })),
    h('div', { class: 'section-title' }, h('div', { class: 'eyebrow' }, 'This phone')),
    h('div', { class: 'list' },
      entry('settings', 'Settings', 'Theme, trackpad, screen, device', () => navigate('/settings')),
      entry('phone', state.device?.name || 'This device', state.server ? `Paired with ${state.server.name}` : 'Paired', () => navigate('/settings'))));

  render();
  const offs = [watch('features', render), watch('device', render)];
  return { destroy: () => offs.forEach((o) => o()) };
}
