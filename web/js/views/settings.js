import { api } from '../api.js';
import { back } from '../router.js';
import { prefs, set, setPref, state, watch } from '../store.js';
import { errorToast, fmtAgo, h, holdButton, icon, segmented, slider, swap, toast, toggle } from '../ui.js';
import { conn } from '../ws.js';
import { certHelp, installSteps } from './pair.js';

const ACCENTS = [
  { id: 'cyan', color: '#22d3ee' },
  { id: 'violet', color: '#a78bfa' },
  { id: 'green', color: '#34d399' },
  { id: 'rose', color: '#fb7185' },
];

function row(label, control, sub) {
  return h('div', { class: 'item set-row' },
    h('div', { class: 'item-body' }, h('div', { class: 'item-title', style: { whiteSpace: 'normal' } }, label), sub && h('div', { class: 'item-sub', style: { whiteSpace: 'normal' } }, sub)),
    control);
}

export default function settings(root) {
  const wrap = h('div', { class: 'wrap' });
  root.append(wrap);
  const info = h('div', { class: 'kv' });

  const nameInput = h('input', { class: 'input', value: state.device?.name || '', maxlength: 48, 'aria-label': 'Device name' });
  const saveName = h('button', { class: 'btn', onclick: async () => {
    const name = nameInput.value.trim();
    if (!name) return;
    try {
      const res = await api.post('/me/rename', { name });
      set({ device: res.device });
      toast('Renamed', { kind: 'ok', timeout: 1400 });
    } catch (err) { errorToast(err); }
  } }, 'Save');

  const swatches = h('div', { class: 'swatches', role: 'radiogroup', 'aria-label': 'Accent colour' });
  const paintSwatches = () => swap(swatches, ACCENTS.map((a) => h('button', {
    class: 'swatch', role: 'radio', 'aria-checked': String(prefs.accent === a.id), 'aria-label': a.id, style: { '--sw': a.color },
    onclick: () => { setPref('accent', a.id); paintSwatches(); },
  })));
  paintSwatches();

  const isIOS = /iP(hone|ad|od)/.test(navigator.userAgent);

  wrap.append(
    h('div', { class: 'section-title' },
      h('button', { class: 'icon-btn back', 'aria-label': 'Back', onclick: () => back('/more') }, icon('chevron-left')),
      h('div', { class: 'h2', style: { flex: 1 } }, 'Settings')),

    h('div', { class: 'section-title' }, h('div', { class: 'eyebrow' }, 'This device')),
    h('div', { class: 'card stack' },
      h('label', { class: 'field' }, h('span', null, 'Name shown on the PC'), h('div', { class: 'row' }, nameInput, saveName))),

    h('div', { class: 'section-title' }, h('div', { class: 'eyebrow' }, 'Look')),
    h('div', { class: 'list' },
      h('div', { class: 'item set-col' }, h('div', { class: 'item-title' }, 'Theme'),
        segmented([{ value: 'system', label: 'Auto' }, { value: 'dark', label: 'Dark' }, { value: 'amoled', label: 'Black' }, { value: 'light', label: 'Light' }],
          prefs.theme, (v) => setPref('theme', v), { label: 'Theme' })),
      row('Accent', swatches),
      row('Animations', toggle(prefs.motion, (v) => setPref('motion', v), 'Animations'), 'Transitions, number tweening and tap feedback.')),

    h('div', { class: 'section-title' }, h('div', { class: 'eyebrow' }, 'Remote')),
    h('div', { class: 'list' },
      h('div', { class: 'item set-col' }, h('div', { class: 'row' }, h('div', { class: 'item-title', style: { flex: 1 } }, 'Trackpad speed'), h('span', { class: 'tiny faint num sens-val' }, `${prefs.sensitivity.toFixed(1)}×`)),
        slider({ min: 0.5, max: 3, step: 0.1, value: prefs.sensitivity, label: 'Trackpad speed',
          onInput: (v) => { wrap.querySelector('.sens-val').textContent = `${v.toFixed(1)}×`; }, onChange: (v) => setPref('sensitivity', v) })),
      h('div', { class: 'item set-col' }, h('div', { class: 'row' }, h('div', { class: 'item-title', style: { flex: 1 } }, 'Scroll speed'), h('span', { class: 'tiny faint num scroll-val' }, `${prefs.scrollSpeed.toFixed(1)}×`)),
        slider({ min: 0.3, max: 3, step: 0.1, value: prefs.scrollSpeed, label: 'Scroll speed',
          onInput: (v) => { wrap.querySelector('.scroll-val').textContent = `${v.toFixed(1)}×`; }, onChange: (v) => setPref('scrollSpeed', v) })),
      h('div', { class: 'item set-col' }, h('div', { class: 'item-title' }, 'Screen quality'),
        segmented([{ value: 'saver', label: 'Data saver' }, { value: 'balanced', label: 'Balanced' }, { value: 'sharp', label: 'Sharp' }],
          prefs.screenPreset, (v) => setPref('screenPreset', v), { label: 'Screen quality' }))),

    isIOS && h('div', { class: 'section-title' }, h('div', { class: 'eyebrow' }, 'Install')),
    isIOS && h('div', { class: 'card' }, installSteps()),
    h('div', { style: { height: '12px' } }),
    certHelp(),

    h('div', { class: 'section-title' }, h('div', { class: 'eyebrow' }, 'About')),
    h('div', { class: 'card' }, info),

    h('div', { class: 'section-title' }, h('div', { class: 'eyebrow' }, 'Danger zone')),
    h('div', { class: 'card stack' },
      h('p', { class: 'small dim' }, 'Unpairs this phone. You’ll need to scan a new pairing code on the PC to come back.'),
      holdButton({ label: 'Disconnect this device', ico: 'log-out', onConfirm: async () => {
        try { await api.post('/me/disconnect'); } catch { /* revoked either way */ }
        conn.unauthorized();
      } })),
    h('p', { class: 'tiny faint', style: { textAlign: 'center', margin: '22px 0 6px' } }, 'LanDeck · made by TK NiRMAL'));

  async function renderInfo() {
    let sys = null;
    try { sys = await api.get('/system/info'); } catch { /* offline */ }
    const s = state.server || {};
    const rows = [
      ['PC', s.name], ['Host', s.hostname], ['Agent', s.version ? `v${s.version}` : null],
      ['Address', (s.ips || []).join(', ')], ['Port', s.port],
      ['Paired as', state.device?.name], ['Paired', state.device ? fmtAgo(state.device.created) : null],
      ...(sys?.macs || []).map((m) => [`MAC (${m.iface})`, m.mac]),
    ];
    swap(info, rows.filter(([, v]) => v != null && v !== '').map(([k, v]) => h('div', { class: 'kv-row' }, h('span', { class: 'faint' }, k), h('span', { class: 'num ellipsis' }, String(v)))),
      sys?.macs?.length ? h('p', { class: 'tiny faint', style: { marginTop: '8px' } }, 'Use the MAC address with a Wake-on-LAN app to switch the PC on remotely.') : null);
  }
  renderInfo();
  const offs = [
    watch('server', renderInfo),
    watch('device', (d) => { if (d && document.activeElement !== nameInput) nameInput.value = d.name; renderInfo(); }),
  ];
  return { refresh: renderInfo, destroy: () => offs.forEach((o) => o()) };
}
