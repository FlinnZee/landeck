import { api } from '../api.js';
import { back } from '../router.js';
import { load, save, state, watch } from '../store.js';
import { copyText, empty, errorToast, h, icon, swap, toast } from '../ui.js';
import { conn } from '../ws.js';

const SESSION_KEY = 'landeck.termSession';
const HISTORY_KEY = 'landeck.termHistory';
const SYMBOLS = ['|', '>', '<', '"', "'", '\\', '/', '~', '-', '$', '*', '.', ';', '&', '{', '}', '[', ']', '(', ')', '='];

export default function terminal(root) {
  const head = h('div', { class: 'section-title term-title' },
    h('button', { class: 'icon-btn back', 'aria-label': 'Back', onclick: () => back('/more') }, icon('chevron-left')),
    h('div', { class: 'h2', style: { flex: 1 } }, 'Terminal'));
  const body = h('div', { class: 'term-host' });
  root.append(head, body);

  let session = sessionStorage.getItem(SESSION_KEY);
  let cwd = '';
  let running = null;
  let history = load(HISTORY_KEY, []);
  let hIndex = history.length;
  const offs = [];

  const headers = () => ({ 'X-Terminal-Session': session || '' });

  function off() {
    swap(body, h('div', { class: 'card' }, empty({
      ico: 'lock',
      text: 'The terminal is off.',
      sub: 'It’s disabled by default because it gives full admin PowerShell. Turn it on and set a PIN in the LanDeck admin page on the PC.',
    })));
  }

  function lockScreen(msg = '') {
    const digits = h('div', { class: 'pin-dots' }, Array.from({ length: 8 }, () => h('i')));
    let pin = '';
    const status = h('div', { class: 'small danger', role: 'status' }, msg);
    const paint = () => [...digits.children].forEach((d, i) => d.classList.toggle('on', i < pin.length));
    const submit = async () => {
      if (pin.length < 6) { status.textContent = 'At least 6 digits.'; return; }
      try {
        const res = await api.post('/terminal/unlock', { pin });
        session = res.session;
        sessionStorage.setItem(SESSION_KEY, session);
        cwd = res.cwd;
        shell();
      } catch (err) {
        pin = '';
        paint();
        status.textContent = err.detail || 'Wrong PIN.';
        digits.animate?.([{ transform: 'translateX(0)' }, { transform: 'translateX(-8px)' }, { transform: 'translateX(8px)' }, { transform: 'translateX(0)' }], { duration: 260 });
      }
    };
    const key = (label, fn, cls = '') => h('button', { class: `pin-key ${cls}`, onclick: fn, 'aria-label': typeof label === 'string' ? label : 'delete' }, label);
    const pad = h('div', { class: 'pin-pad' },
      [1, 2, 3, 4, 5, 6, 7, 8, 9].map((n) => key(String(n), () => { if (pin.length < 8) { pin += n; paint(); status.textContent = ''; } })),
      key(icon('backspace'), () => { pin = pin.slice(0, -1); paint(); }, 'ghost'),
      key('0', () => { if (pin.length < 8) { pin += '0'; paint(); } }),
      key(icon('arrow-right'), submit, 'go'));
    swap(body, h('div', { class: 'card pin-card' },
      h('div', { class: 'item-icon accent big-icon' }, icon('lock')),
      h('div', { class: 'h3' }, 'Enter the terminal PIN'),
      h('div', { class: 'small faint' }, 'Set on the PC in the admin page.'),
      digits, status, pad));
  }

  function shell() {
    const out = h('pre', { class: 'term-out', 'aria-live': 'polite' });
    const cwdEl = h('div', { class: 'term-cwd mono ellipsis' }, cwd);
    const input = h('textarea', { class: 'term-input mono', rows: 1, placeholder: 'PowerShell command', autocapitalize: 'off', autocomplete: 'off', autocorrect: 'off', spellcheck: false, enterkeyhint: 'send', 'aria-label': 'Command' });
    const runBtn = h('button', { class: 'btn primary square', 'aria-label': 'Run', onclick: run }, icon('arrow-right'));
    const cancelBtn = h('button', { class: 'btn danger small', hidden: true, onclick: cancel }, icon('x'), 'Ctrl+C');
    const symbols = h('div', { class: 'chips term-symbols' }, SYMBOLS.map((s) => h('button', { class: 'chip mono', onclick: () => insert(s) }, s)),
      h('button', { class: 'chip', onclick: () => recall(-1), 'aria-label': 'Previous command' }, icon('arrow-up')),
      h('button', { class: 'chip', onclick: () => recall(1), 'aria-label': 'Next command' }, icon('arrow-down')));

    swap(body,
      h('div', { class: 'admin-banner', role: 'alert' }, icon('shield'), h('b', null, 'ADMIN SHELL'), h('span', { class: 'small' }, 'Commands run elevated on the PC and are logged.')),
      h('div', { class: 'term-bar' }, cwdEl,
        h('button', { class: 'icon-btn', 'aria-label': 'Copy output', onclick: async () => { toast((await copyText(out.textContent)) ? 'Output copied' : 'Couldn’t copy', { timeout: 1400 }); } }, icon('copy')),
        h('button', { class: 'icon-btn', 'aria-label': 'Clear', onclick: () => { out.textContent = ''; } }, icon('trash')),
        h('button', { class: 'icon-btn', 'aria-label': 'Lock terminal', onclick: lock }, icon('lock'))),
      out,
      h('div', { class: 'term-compose' }, symbols, h('div', { class: 'row' }, input, cancelBtn, runBtn)));

    const write = (text, cls) => {
      const span = h('span', { class: cls }, text);
      out.append(span);
      out.scrollTop = out.scrollHeight;
    };
    function insert(s) {
      const { selectionStart: a, selectionEnd: b, value } = input;
      input.value = value.slice(0, a) + s + value.slice(b);
      input.setSelectionRange(a + s.length, a + s.length);
      input.focus();
    }
    function recall(dir) {
      if (!history.length) return;
      hIndex = Math.max(0, Math.min(history.length, hIndex + dir));
      input.value = history[hIndex] || '';
      input.focus();
    }
    async function run() {
      const command = input.value.trim();
      if (!command || running) return;
      try {
        write(`PS ${cwd}> `, 'term-prompt');
        write(`${command}\n`, 'term-cmd');
        const res = await api.post('/terminal/run', { command, confirm: true }, { headers: headers() });
        running = res.run;
        history = [...history.filter((c) => c !== command), command].slice(-100);
        save(HISTORY_KEY, history);
        hIndex = history.length;
        input.value = '';
        cancelBtn.hidden = false;
        runBtn.disabled = true;
      } catch (err) {
        if (err.error === 'locked') { session = null; sessionStorage.removeItem(SESSION_KEY); lockScreen(err.detail); return; }
        write(`${err.detail || 'Failed'}\n`, 'term-err');
      }
    }
    async function cancel() {
      if (!running) return;
      try { await api.post('/terminal/cancel', { run: running }, { headers: headers() }); } catch (err) { errorToast(err); }
    }
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); run(); }
      if (e.key === 'ArrowUp' && !input.value.includes('\n')) { e.preventDefault(); recall(-1); }
      if (e.key === 'ArrowDown' && !input.value.includes('\n')) { e.preventDefault(); recall(1); }
    });
    offs.push(conn.on('terminal', (m) => {
      if (m.run !== running) return;
      if (m.kind === 'out') write(m.data);
      if (m.kind === 'exit') {
        if (m.code) write(`[exit ${m.code}]\n`, 'term-err');
        running = null;
        cancelBtn.hidden = true;
        runBtn.disabled = false;
        cwd = m.cwd || cwd;
        cwdEl.textContent = cwd;
      }
    }));
    write('LanDeck PowerShell. Each command runs in a fresh process; the working folder carries over.\n', 'term-dim');
    setTimeout(() => input.focus(), 200);
  }

  async function lock() {
    try { await api.post('/terminal/lock', {}, { headers: headers() }); } catch { /* */ }
    session = null;
    sessionStorage.removeItem(SESSION_KEY);
    lockScreen();
  }

  async function init() {
    if (!state.features.terminal) { off(); return; }
    try {
      const st = await api.get('/terminal', { headers: headers() });
      if (!st.pin_set) {
        swap(body, h('div', { class: 'card' }, empty({ ico: 'lock', text: 'No PIN set yet.', sub: 'Set a terminal PIN in the admin page on the PC first.' })));
        return;
      }
      if (st.unlocked) { cwd = st.cwd; shell(); } else lockScreen();
    } catch (err) {
      if (err.error === 'feature_disabled') off();
      else swap(body, h('div', { class: 'card' }, empty({ ico: 'alert', text: err.detail || 'Terminal unavailable.' })));
    }
  }

  offs.push(watch('features', (f) => { if (!f.terminal) off(); }));
  init();
  return { refresh: init, destroy: () => offs.forEach((o) => o()) };
}
