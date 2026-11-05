import { conn } from './ws.js';

export class ApiError extends Error {
  constructor(status, error, detail, extra = {}) {
    super(detail || error || `HTTP ${status}`);
    this.status = status;
    this.error = error;
    this.detail = detail;
    Object.assign(this, extra);
  }
}

export function url(path) {
  if (/^https?:/.test(path)) return path;
  return conn.base + (path.startsWith('/api/') ? path : `/api/v1${path}`);
}

export async function request(path, { method = 'GET', body, headers = {}, signal, raw = false, timeout = 20000 } = {}) {
  const h = { 'X-LanDeck': '1', ...headers };
  if (conn.token) h.Authorization = `Bearer ${conn.token}`;
  let payload = body;
  if (body != null && !(body instanceof Blob) && !(body instanceof ArrayBuffer) && !(body instanceof FormData)) {
    h['Content-Type'] = 'application/json';
    payload = JSON.stringify(body);
  }
  const ctrl = new AbortController();
  const onAbort = () => ctrl.abort();
  signal?.addEventListener('abort', onAbort);
  const timer = timeout ? setTimeout(() => ctrl.abort(), timeout) : null;
  let res;
  try {
    res = await fetch(url(path), { method, headers: h, body: payload, signal: ctrl.signal, cache: 'no-store' });
  } catch (err) {
    if (signal?.aborted) throw err;
    conn.poke();
    throw new ApiError(0, 'network', 'Can’t reach the PC right now.');
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', onAbort);
  }
  if (raw && res.ok) return res;
  let data = null;
  const type = res.headers.get('content-type') || '';
  if (type.includes('json')) data = await res.json().catch(() => null);
  if (!res.ok) {
    const err = new ApiError(res.status, data?.error || 'http_error', data?.detail || res.statusText, data || {});
    if (res.status === 401 && data?.error === 'unauthorized') conn.unauthorized();
    throw err;
  }
  return data ?? res;
}

export const api = {
  get: (p, o) => request(p, o),
  post: (p, body, o) => request(p, { ...o, method: 'POST', body: body ?? {} }),
  put: (p, body, o) => request(p, { ...o, method: 'PUT', body }),
  patch: (p, body, o) => request(p, { ...o, method: 'PATCH', body }),
  del: (p, body, o) => request(p, { ...o, method: 'DELETE', body }),
  blob: async (p, o) => (await request(p, { ...o, raw: true })).blob(),
};
