// Tiny reactive store: one shared state object, keyed subscriptions, and persisted prefs.

const listeners = new Map(); // key -> Set<fn>
const any = new Set();

export const state = {
  conn: 'connecting',      // connecting | online | reconnecting | offline | waiting
  device: null,
  server: null,
  features: {},
  notifications: [],
  unread: 0,
  moved: null,             // set when we reached the PC on a different origin than the page
};

export function set(patch) {
  const changed = [];
  for (const [k, v] of Object.entries(patch)) {
    if (state[k] !== v) {
      state[k] = v;
      changed.push(k);
    }
  }
  for (const k of changed) listeners.get(k)?.forEach((fn) => fn(state[k], state));
  if (changed.length) any.forEach((fn) => fn(changed, state));
}

export function watch(key, fn, { now = false } = {}) {
  if (!listeners.has(key)) listeners.set(key, new Set());
  listeners.get(key).add(fn);
  if (now) fn(state[key], state);
  return () => listeners.get(key)?.delete(fn);
}

export function onAny(fn) {
  any.add(fn);
  return () => any.delete(fn);
}

// --- prefs (per device, localStorage) --------------------------------------

const PREF_KEY = 'landeck.prefs';
const DEFAULT_PREFS = {
  theme: 'system',         // system | dark | amoled | light
  accent: 'cyan',
  motion: true,
  sensitivity: 1.4,
  scrollSpeed: 1,
  screenPreset: 'balanced',
  screenFps: 8,
  onboarded: false,
  filesHidden: false,
  filesSort: 'name',
  procSort: 'cpu',
};

function readPrefs() {
  try {
    return { ...DEFAULT_PREFS, ...JSON.parse(localStorage.getItem(PREF_KEY) || '{}') };
  } catch {
    return { ...DEFAULT_PREFS };
  }
}

export const prefs = readPrefs();

export function setPref(key, value) {
  prefs[key] = value;
  try { localStorage.setItem(PREF_KEY, JSON.stringify(prefs)); } catch { /* private mode */ }
  listeners.get(`pref:${key}`)?.forEach((fn) => fn(value));
}

export function watchPref(key, fn) {
  return watch(`pref:${key}`, fn);
}

// --- small local key/value helpers -----------------------------------------

export function load(key, fallback = null) {
  try {
    const raw = localStorage.getItem(key);
    return raw == null ? fallback : JSON.parse(raw);
  } catch {
    return fallback;
  }
}

export function save(key, value) {
  try {
    if (value == null) localStorage.removeItem(key);
    else localStorage.setItem(key, JSON.stringify(value));
  } catch { /* ignore */ }
}
