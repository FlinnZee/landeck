import { ApiError } from '../api.js';
import { brandMark, icon } from '../icons.js';
import { setPref } from '../store.js';
import { h, swap } from '../ui.js';
import { conn } from '../ws.js';

const isIOS = /iP(hone|ad|od)/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
const standalone = matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;

function guessName() {
  const ua = navigator.userAgent;
  if (/iPhone/.test(ua)) return 'iPhone';
  if (/iPad/.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1)) return 'iPad';
  if (/Android/.test(ua)) return 'Android phone';
  if (/Mac/.test(ua)) return 'Mac';
  return 'Browser';
}

export function installSteps() {
  return h('ol', { class: 'steps' },
    h('li', null, h('span', { class: 'step-n' }, '1'), h('span', null, 'Tap ', h('b', null, 'Share'), ' ', icon('share', 'inline'), ' in Safari’s toolbar.')),
    h('li', null, h('span', { class: 'step-n' }, '2'), h('span', null, 'Scroll and pick ', h('b', null, 'Add to Home Screen'), ' ', icon('plus-square', 'inline'), '.')),
    h('li', null, h('span', { class: 'step-n' }, '3'), h('span', null, 'Open LanDeck from your Home Screen. It runs full screen, like an app.')));
}

export function certHelp() {
  return h('details', { class: 'card help' },
    h('summary', null, icon('shield'), h('span', null, 'Seeing “This connection is not private”?')),
    h('div', { class: 'stack small dim', style: { marginTop: '10px' } },
      h('p', null, 'That’s expected the first time: the PC uses its own certificate, not one from a public authority.'),
      h('p', null, 'Tap ', h('b', null, 'Show Details'), ' → ', h('b', null, 'visit this website'), ' → ', h('b', null, 'Visit Website'), '.'),
      h('p', null, 'To make the warning go away for good, install the LanDeck certificate profile:'),
      h('a', { class: 'btn small', href: '/ca.mobileconfig' }, icon('download'), 'Get certificate profile'),
      h('p', null, 'Then open ', h('b', null, 'Settings → General → VPN & Device Management'), ' to install it, and switch it on under ',
        h('b', null, 'Settings → General → About → Certificate Trust Settings'), '. It can only vouch for local addresses, never real websites.')));
}

// --- onboarding ------------------------------------------------------------

export function showOnboarding(root, done) {
  const cards = [
    { ico: 'scan', title: 'Pair with your PC', text: 'On the PC, click the LanDeck tray icon → Pair new device, then scan the QR code with your camera.' },
    { ico: 'plus-square', title: 'Add to Home Screen', text: 'LanDeck is a web app. Added to the Home Screen it opens full screen and remembers the pairing.', extra: isIOS && !standalone ? installSteps() : null },
    { ico: 'sparkles', title: 'A few tips', text: 'It reconnects on its own after sleep or Wi-Fi changes. Dangerous buttons need a press-and-hold. Everything stays on your local network.' },
  ];
  let i = 0;
  const dots = h('div', { class: 'dots' }, cards.map(() => h('i')));
  const body = h('div', { class: 'onb-card' });
  const next = h('button', { class: 'btn primary block' });
  const skip = h('button', { class: 'btn ghost block', onclick: finish }, 'Skip');
  function finish() {
    setPref('onboarded', true);
    done();
  }
  function show() {
    const c = cards[i];
    swap(body,
      h('div', { class: 'onb-icon' }, icon(c.ico)),
      h('h1', { class: 'h1' }, c.title),
      h('p', { class: 'dim' }, c.text),
      c.extra);
    body.classList.remove('view-enter');
    void body.offsetWidth;
    body.classList.add('view-enter');
    [...dots.children].forEach((d, k) => d.classList.toggle('on', k === i));
    next.textContent = i === cards.length - 1 ? 'Get started' : 'Next';
  }
  next.addEventListener('click', () => { if (i < cards.length - 1) { i++; show(); } else finish(); });
  swap(root, h('div', { class: 'solo' },
    h('div', { class: 'solo-inner' },
      h('div', { class: 'row', style: { justifyContent: 'center', marginBottom: '8px' } }, brandMark('brand-mark big')),
      body, dots,
      h('div', { class: 'stack', style: { marginTop: 'auto' } }, next, skip))));
  show();
}

// --- pairing -----------------------------------------------------------------

export function showPair(root, { onPaired, secret = '', revoked = false } = {}) {
  const name = h('input', { class: 'input', value: guessName(), maxlength: 48, autocomplete: 'off', 'aria-label': 'Device name' });
  const codeBoxes = Array.from({ length: 6 }, (_, k) => h('input', {
    class: 'otp', inputmode: 'numeric', maxlength: 1, autocomplete: k === 0 ? 'one-time-code' : 'off', 'aria-label': `Digit ${k + 1}`,
  }));
  const status = h('div', { class: 'pair-status', role: 'status' });
  const submit = h('button', { class: 'btn primary block' }, 'Pair this device');

  codeBoxes.forEach((box, k) => {
    box.addEventListener('input', () => {
      const digits = box.value.replace(/\D/g, '');
      if (digits.length > 1) {
        // pasted or autofilled the whole code
        digits.slice(0, 6).split('').forEach((d, j) => { codeBoxes[j].value = d; });
        codeBoxes[Math.min(5, digits.length - 1)].focus();
        return;
      }
      box.value = digits;
      if (digits && k < 5) codeBoxes[k + 1].focus();
    });
    box.addEventListener('keydown', (e) => {
      if (e.key === 'Backspace' && !box.value && k > 0) codeBoxes[k - 1].focus();
      if (e.key === 'Enter') go();
    });
  });

  const setStatus = (kind, text, ico) => {
    swap(status, text && h('div', { class: `notice ${kind}` }, kind === 'wait' ? h('div', { class: 'spinner' }) : icon(ico || 'info'), h('div', null, text)));
  };

  async function go() {
    const code = codeBoxes.map((b) => b.value).join('');
    if (!secret && code.length !== 6) {
      setStatus('warn', 'Enter the 6-digit code shown on the PC.', 'alert');
      codeBoxes[code.length]?.focus();
      return;
    }
    if (!name.value.trim()) {
      setStatus('warn', 'Give this device a name so you can recognise it later.', 'alert');
      name.focus();
      return;
    }
    submit.disabled = true;
    setStatus('wait', 'Waiting for you to click Yes on the PC…');
    try {
      const res = await fetch('/api/v1/pair', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-LanDeck': '1' },
        body: JSON.stringify(secret ? { secret, name: name.value.trim() } : { code, name: name.value.trim() }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new ApiError(res.status, data.error, data.detail);
      conn.setToken(data.token);
      setPref('onboarded', true);
      setStatus('ok', `Paired as “${data.device.name}”.`, 'check');
      history.replaceState(null, '', '/#/home');
      setTimeout(onPaired, 450);
    } catch (err) {
      submit.disabled = false;
      const text = err instanceof ApiError ? err.detail : 'Can’t reach the PC. Check that both are on the same Wi-Fi.';
      setStatus('danger', text, 'alert');
      if (err.error === 'pairing_invalid' || err.error === 'pairing_expired') {
        secret = '';
        codeWrap.hidden = false;
        codeBoxes.forEach((b) => { b.value = ''; });
      }
    }
  }
  submit.addEventListener('click', go);

  const codeWrap = h('div', { class: 'field' }, h('span', null, 'Pairing code from the PC'), h('div', { class: 'otp-row' }, codeBoxes));
  codeWrap.hidden = !!secret;

  swap(root, h('div', { class: 'solo' },
    h('div', { class: 'solo-inner' },
      h('div', { class: 'row', style: { gap: '14px', marginBottom: '6px' } },
        brandMark('brand-mark big'),
        h('div', null, h('h1', { class: 'h1' }, 'Pair device'), h('div', { class: 'dim small' }, location.host))),
      revoked && h('div', { class: 'notice danger' }, icon('ban'), h('div', null, h('b', null, 'This device was revoked.'), ' Pair it again from the PC to keep using LanDeck.')),
      h('p', { class: 'dim' }, secret
        ? 'You scanned a pairing code. Name this device and confirm on the PC.'
        : 'On the PC: tray icon → Pair new device. Scan the QR, or type the 6-digit code here.'),
      h('label', { class: 'field' }, h('span', null, 'Name this device'), name),
      codeWrap,
      status,
      submit,
      certHelp(),
      isIOS && !standalone && h('details', { class: 'card help' },
        h('summary', null, icon('plus-square'), h('span', null, 'Install as an app')), h('div', { style: { marginTop: '10px' } }, installSteps())))));
  if (!secret) setTimeout(() => codeBoxes[0].focus(), 300);
}
