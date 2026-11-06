// One connection manager for the whole app: finds the PC, keeps the socket alive,
// and reconnects on its own after sleep, Wi-Fi drops, IP changes or agent restarts.
import { load, save, set, state } from './store.js';

const TOKEN_KEY = 'landeck.token';
const LAST_KEY = 'landeck.last';
const MAX_DELAY = 10000;

class Connection {
  constructor() {
    this.token = load(TOKEN_KEY);
    this.base = location.origin;
    this.ws = null;
    this.handlers = new Map();
    this.topics = new Map();
    this.attempt = 0;
    this.timer = null;
    this.lastMsg = 0;
    this.failingSince = 0;
    this.everOnline = false;
    this.expectOffline = false;
    this.connecting = false;
    this.started = false;
  }

  // --- events -------------------------------------------------------------

  on(type, fn) {
    if (!this.handlers.has(type)) this.handlers.set(type, new Set());
    this.handlers.get(type).add(fn);
    return () => this.handlers.get(type)?.delete(fn);
  }

  emit(type, data) {
    this.handlers.get(type)?.forEach((fn) => {
      try { fn(data); } catch (err) { console.error(err); }
    });
  }

  // --- token ----------------------------------------------------------------

  setToken(token) {
    this.token = token;
    save(TOKEN_KEY, token);
  }

  unauthorized() {
    const had = !!this.token;
    this.setToken(null);
    clearTimeout(this.timer);
    const ws = this.ws;
    this.ws = null;
    if (ws) try { ws.close(); } catch { /* already gone */ }
    set({ conn: 'offline' });
    if (had) this.emit('unauthorized');
  }

  // --- lifecycle --------------------------------------------------------------

  start() {
    if (!this.started) {
      this.started = true;
      document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') this.now();
      });
      window.addEventListener('online', () => this.now());
      window.addEventListener('pageshow', () => this.now());
      setInterval(() => this.watchdog(), 3000);
    }
    this.connect();
  }

  candidates() {
    const last = load(LAST_KEY) || {};
    const port = state.server?.port || last.port || location.port || 8765;
    // landeck.local first: on iOS a raw private IP over a self-signed cert is flaky,
    // while the mDNS name is treated as local and connects reliably.
    const mdns = `https://landeck.local:${port}`;
    const list = [mdns, location.origin];
    if (last.origin) list.push(last.origin);
    for (const ip of state.server?.ips || last.ips || []) list.push(`https://${ip}:${port}`);
    return [...new Set(list)];
  }

  async pick() {
    const cands = this.candidates();
    const mdns = cands.find((c) => c.includes('landeck.local'));
    // probe every address at once so a dead/old origin can't delay finding a live one
    const results = await Promise.all(cands.map(async (c) => ({ c, ok: await this.probe(c) })));
    const good = new Set(results.filter((r) => r.ok).map((r) => r.c));
    if (mdns && good.has(mdns)) return mdns;
    return cands.find((c) => good.has(c)) || null;
  }

  async probe(origin) {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 2500);
    try {
      const res = await fetch(`${origin}/api/v1/health`, { signal: ctrl.signal, cache: 'no-store' });
      return res.ok;
    } catch {
      return false;
    } finally {
      clearTimeout(t);
    }
  }

  async connect() {
    if (this.connecting || !this.token) return;
    if (this.ws && this.ws.readyState <= 1) return;
    clearTimeout(this.timer);
    this.connecting = true;
    if (state.conn === 'online') set({ conn: 'reconnecting' });
    try {
      const origin = await this.pick();
      if (!origin) throw new Error('unreachable');
      await this.open(origin);
    } catch {
      this.connecting = false;
      if (this.token) this.retry();
      return;
    }
    this.connecting = false;
  }

  open(origin) {
    return new Promise((resolve, reject) => {
      let settled = false;
      const ws = new WebSocket(`${origin.replace(/^http/, 'ws')}/ws`);
      ws.binaryType = 'arraybuffer';
      this.ws = ws;
      const fail = (err) => {
        if (settled) return;
        settled = true;
        clearTimeout(timeout);
        if (this.ws === ws) this.ws = null;
        reject(err);
      };
      const timeout = setTimeout(() => { fail(new Error('timeout')); try { ws.close(); } catch { /* */ } }, 6000);

      ws.onopen = () => ws.send(JSON.stringify({ type: 'auth', token: this.token }));
      ws.onmessage = (ev) => {
        this.lastMsg = Date.now();
        if (typeof ev.data !== 'string') {
          this.emit('frame', ev.data);
          return;
        }
        let msg;
        try { msg = JSON.parse(ev.data); } catch { return; }
        if (msg.type === 'ping') {
          ws.send(JSON.stringify({ type: 'pong', t: msg.t }));
          return;
        }
        if (msg.type === 'hello' && !settled) {
          settled = true;
          clearTimeout(timeout);
          this.onHello(origin, msg.data);
          resolve();
          return;
        }
        if (msg.type === 'power' && msg.data?.state === 'going_offline') this.expectOffline = true;
        this.emit(msg.type, msg.data);
      };
      ws.onclose = (ev) => {
        if (!settled) {
          fail(new Error(`closed ${ev.code}`));
          if (ev.code === 4401) this.unauthorized();
          return;
        }
        if (this.ws !== ws) return;
        this.ws = null;
        if (ev.code === 4401) { this.unauthorized(); return; }
        this.emit('closed', ev.code);
        this.retry();
      };
      ws.onerror = () => { /* onclose follows */ };
    });
  }

  onHello(origin, hello) {
    const wasOnline = this.everOnline;
    this.base = origin;
    this.attempt = 0;
    this.failingSince = 0;
    this.everOnline = true;
    this.expectOffline = false;
    save(LAST_KEY, { origin, port: hello.server.port, ips: hello.server.ips });
    set({
      conn: 'online',
      device: hello.device,
      server: hello.server,
      features: hello.features,
      moved: origin !== location.origin ? origin : null,
    });
    const topics = [...this.topics.keys()];
    if (topics.length) this.send({ type: 'sub', topics });
    this.emit('online', { reconnect: wasOnline });
  }

  retry() {
    clearTimeout(this.timer);
    this.attempt += 1;
    if (!this.failingSince) this.failingSince = Date.now();
    const delay = Math.min(MAX_DELAY, 500 * 2 ** (this.attempt - 1)) * (0.75 + Math.random() * 0.5);
    let next = this.everOnline ? 'reconnecting' : 'connecting';
    if (this.expectOffline) next = 'waiting';
    else if (Date.now() - this.failingSince > 20000) next = 'offline';
    set({ conn: next });
    this.timer = setTimeout(() => this.connect(), delay);
  }

  // reconnect right away (app foregrounded, network back, user tapped the pill)
  now() {
    if (!this.token) return;
    if (this.ws && this.ws.readyState === 1) {
      // iOS may have frozen the socket while we were in the background
      if (Date.now() - this.lastMsg > 7000) {
        try { this.ws.close(); } catch { /* */ }
        this.ws = null;
      } else return;
    }
    this.attempt = 0;
    clearTimeout(this.timer);
    this.connect();
  }

  poke() {
    if (this.ws && this.ws.readyState === 1 && Date.now() - this.lastMsg > 6000) this.now();
  }

  watchdog() {
    if (this.ws && this.ws.readyState === 1 && Date.now() - this.lastMsg > 15000) {
      try { this.ws.close(4000); } catch { /* */ }
    }
  }

  // --- messaging --------------------------------------------------------------

  get online() {
    return state.conn === 'online';
  }

  send(msg) {
    if (this.ws && this.ws.readyState === 1) {
      this.ws.send(JSON.stringify(msg));
      return true;
    }
    return false;
  }

  sub(topic) {
    const n = this.topics.get(topic) || 0;
    this.topics.set(topic, n + 1);
    if (n === 0) this.send({ type: 'sub', topics: [topic] });
    return () => {
      const left = (this.topics.get(topic) || 1) - 1;
      if (left <= 0) {
        this.topics.delete(topic);
        this.send({ type: 'unsub', topics: [topic] });
      } else this.topics.set(topic, left);
    };
  }
}

export const conn = new Connection();
