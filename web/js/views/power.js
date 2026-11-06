import { api } from '../api.js';
import { state } from '../store.js';
import { disabledCard, errorToast, fmtClock, h, holdButton, icon, swap, tap, toast } from '../ui.js';
import { conn } from '../ws.js';

const LABEL = { shutdown: 'Shut down', restart: 'Restart', sleep: 'Sleep', hibernate: 'Hibernate', lock: 'Lock', signout: 'Sign out' };

export default function power(root) {
  if (state.features.power === false) {
    root.append(disabledCard('power', 'Power control'));
    return {};
  }
  let info = null;
  let countdown = null;

  const quick = h('div', { class: 'grid-2' },
    h('button', { class: 'tile', onclick: (e) => quickAct(e.currentTarget, 'lock') }, h('div', { class: 'tile-icon' }, icon('lock')), h('div', null, h('b', null, 'Lock'), h('div', { class: 'tiny faint' }, 'Back to the sign-in screen'))),
    h('button', { class: 'tile', onclick: (e) => displayOff(e.currentTarget) }, h('div', { class: 'tile-icon' }, icon('monitor-off')), h('div', null, h('b', null, 'Display off'), h('div', { class: 'tiny faint' }, 'Any input wakes it'))),
    h('button', { class: 'tile', onclick: (e) => displayOn(e.currentTarget) }, h('div', { class: 'tile-icon accent' }, icon('sun')), h('div', null, h('b', null, 'Wake display'), h('div', { class: 'tiny faint' }, 'Turn the screen back on'))));

  const holds = h('div', { class: 'stack' });
  const sched = h('div', { class: 'card' });
  const scheduled = h('div', { class: 'card accent-edge countdown', hidden: true });
  root.append(
    h('div', { class: 'section-title' }, h('div', { class: 'eyebrow' }, 'Quick')), quick,
    scheduled,
    h('div', { class: 'section-title' }, h('div', { class: 'eyebrow' }, 'Power'), h('div', { class: 'tiny faint' }, 'press and hold')), holds,
    h('div', { class: 'section-title' }, h('div', { class: 'eyebrow' }, 'Schedule')), sched);

  async function quickAct(btn, action) {
    tap(btn);
    try {
      await api.post(`/power/${action}`, { confirm: true });
      toast(`${LABEL[action]} sent`, { kind: 'ok', timeout: 1800 });
    } catch (err) { errorToast(err); }
  }

  async function displayOff(btn) {
    tap(btn);
    try {
      await api.post('/power/display-off');
      toast('Display off', { kind: 'ok', text: 'Tap Wake display to turn it back on.', timeout: 2200 });
    } catch (err) { errorToast(err); }
  }

  async function displayOn(btn) {
    tap(btn);
    try {
      await api.post('/power/display-on');
      toast('Display on', { kind: 'ok', timeout: 1600 });
    } catch (err) { errorToast(err); }
  }

  async function act(action) {
    try {
      await api.post(`/power/${action}`, { confirm: true });
      toast(`${LABEL[action]}…`, { kind: 'ok', text: 'The phone will reconnect when the PC is back.' });
    } catch (err) { errorToast(err); }
  }

  function renderHolds() {
    const caps = info?.capabilities || { sleep: true, hibernate: false };
    const list = [
      ['sleep', 'moon', caps.sleep],
      ['restart', 'restart', true],
      ['shutdown', 'power', true],
      ['signout', 'log-out', true],
      ['hibernate', 'clock', caps.hibernate],
    ].filter(([, , ok]) => ok);
    const gentle = new Set(['sleep', 'hibernate']);
    swap(holds, h('div', { class: 'grid-2' }, list.map(([a, ico]) => holdButton({ label: LABEL[a], ico, kind: gentle.has(a) ? 'soft' : 'danger', className: 'power-btn', onConfirm: () => act(a) }))),
      info?.capabilities?.modern_standby && h('p', { class: 'tiny faint' }, 'This PC uses Modern Standby: Sleep turns the display off and Windows takes it from there.'));
  }

  // --- schedule -----------------------------------------------------------------------

  let schedAction = 'shutdown';
  let minutes = 30;

  function renderSchedule() {
    const actions = ['shutdown', 'restart', 'sleep'].concat(info?.capabilities?.hibernate ? ['hibernate'] : []);
    const custom = h('input', { class: 'input mins', type: 'number', inputmode: 'numeric', min: 1, max: 10080, value: minutes, 'aria-label': 'Minutes' });
    custom.addEventListener('input', () => { minutes = Math.max(1, Number(custom.value) || 1); paintPresets(); });
    const presets = h('div', { class: 'chips' });
    const paintPresets = () => swap(presets, [15, 30, 60, 120, 240].map((m) => h('button', {
      class: 'chip', 'aria-pressed': String(minutes === m), onclick: () => { minutes = m; custom.value = m; paintPresets(); },
    }, m < 60 ? `${m} min` : `${m / 60} h`)));
    paintPresets();
    swap(sched,
      h('div', { class: 'chips', style: { marginBottom: '12px' } }, actions.map((a) => h('button', {
        class: 'chip', 'aria-pressed': String(schedAction === a), onclick: () => { schedAction = a; renderSchedule(); },
      }, LABEL[a]))),
      presets,
      h('div', { class: 'row', style: { marginTop: '12px' } },
        h('span', { class: 'dim small' }, 'in'), custom, h('span', { class: 'dim small' }, 'minutes'), h('div', { class: 'spacer' }),
        holdButton({ label: 'Schedule', kind: 'primary', ico: 'timer', duration: 800, onConfirm: schedule })));
  }

  async function schedule() {
    try {
      const res = await api.post('/power/schedule', { action: schedAction, in_minutes: minutes, confirm: true });
      info = { ...info, schedule: res.schedule };
      renderCountdown();
      toast(`${LABEL[schedAction]} scheduled`, { kind: 'ok', text: `in ${minutes} min` });
    } catch (err) { errorToast(err); }
  }

  async function cancel() {
    try {
      await api.del('/power/schedule');
      info = { ...info, schedule: null };
      renderCountdown();
      toast('Schedule cancelled', { kind: 'ok' });
    } catch (err) { errorToast(err); }
  }

  function renderCountdown() {
    clearInterval(countdown);
    const s = info?.schedule;
    scheduled.hidden = !s;
    if (!s) return;
    const clock = h('div', { class: 'count num' });
    const skew = (info.now ? info.now * 1000 - Date.now() : 0);
    const tick = () => {
      const left = s.due - (Date.now() + skew) / 1000;
      clock.textContent = fmtClock(Math.max(0, left));
      if (left <= 0) clearInterval(countdown);
    };
    swap(scheduled,
      h('div', { class: 'row' }, h('div', { class: 'tile-icon accent' }, icon('timer')),
        h('div', { style: { flex: 1 } }, h('div', { class: 'eyebrow' }, `${LABEL[s.action]} scheduled`), clock),
        h('button', { class: 'btn danger small', onclick: cancel }, icon('x'), 'Cancel')));
    tick();
    countdown = setInterval(tick, 1000);
  }

  async function load() {
    try {
      info = await api.get('/power/info');
    } catch { /* keep defaults */ }
    renderHolds();
    renderSchedule();
    renderCountdown();
  }

  const off = conn.on('power', (p) => {
    if (p.state === 'scheduled') { info = { ...info, schedule: p.schedule, now: Date.now() / 1000 }; renderCountdown(); }
    if (p.state === 'schedule_cancelled') { info = { ...info, schedule: null }; renderCountdown(); }
  });
  renderHolds();
  renderSchedule();
  load();
  return { refresh: load, destroy() { off(); clearInterval(countdown); } };
}
