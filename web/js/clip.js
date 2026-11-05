// Clipboard helpers shared by the shell and the clipboard view.
import { api } from './api.js';
import { load, save } from './store.js';
import { copyText, toast } from './ui.js';

// --- clipboard outbox (queued while offline) ---------------------------------

const OUTBOX = 'landeck.outbox';

export function queueClipboard(text) {
  const box = load(OUTBOX, []);
  box.push({ text, ts: Date.now() });
  save(OUTBOX, box.slice(-20));
}

export async function flushOutbox() {
  const box = load(OUTBOX, []);
  if (!box.length) return;
  save(OUTBOX, []);
  let sent = 0;
  for (const item of box) {
    try { await api.post('/clipboard', { text: item.text }); sent++; } catch { queueClipboard(item.text); }
  }
  if (sent) toast(`Sent ${sent} queued clip${sent > 1 ? 's' : ''} to the PC`, { kind: 'ok' });
}

export async function copyFromPc(itemId) {
  // Safari keeps the user gesture alive for a ClipboardItem built from a promise
  const fetchText = () => api.get(`/clipboard/item/${itemId}`).then((r) => r.text);
  if (window.ClipboardItem && navigator.clipboard?.write) {
    try {
      await navigator.clipboard.write([new ClipboardItem({ 'text/plain': fetchText().then((t) => new Blob([t], { type: 'text/plain' })) })]);
      toast('Copied to this phone', { kind: 'ok', timeout: 1500 });
      return;
    } catch { /* fall back below */ }
  }
  const ok = await copyText(await fetchText());
  toast(ok ? 'Copied to this phone' : 'Couldn’t copy', { kind: ok ? 'ok' : 'error', timeout: 1500 });
}
