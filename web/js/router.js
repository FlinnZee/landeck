// Hash router. Each view is a function (el, params) -> { refresh?, destroy?, title? }.
import { h } from './ui.js';

const routes = [];
let host = null;
let active = null;
let onChange = () => {};

export function route(pattern, view, { tab, fill = false, title = '' } = {}) {
  routes.push({ pattern, view, tab, fill, title });
}

export function parse(hash = location.hash) {
  const raw = hash.replace(/^#/, '') || '/home';
  const [path, qs = ''] = raw.split('?');
  return { path, query: Object.fromEntries(new URLSearchParams(qs)) };
}

export function navigate(path, { replace = false } = {}) {
  const target = `#${path}`;
  if (location.hash === target) return render();
  if (replace) {
    history.replaceState(null, '', target);
    render();
  } else location.hash = target;
}

export function back(fallback = '/home') {
  if (history.length > 1) history.back();
  else navigate(fallback, { replace: true });
}

export function current() {
  return active;
}

export function refreshActive(info) {
  active?.instance?.refresh?.(info);
}

export function start(el, changed) {
  host = el;
  onChange = changed || onChange;
  window.addEventListener('hashchange', render);
  render();
}

export function render() {
  if (!host) return;
  const { path, query } = parse();
  let match = null;
  let found = null;
  for (const r of routes) {
    match = path.match(r.pattern);
    if (match) { found = r; break; }
  }
  if (!found) {
    navigate('/home', { replace: true });
    return;
  }
  // sub-route change inside the same view (e.g. /files/a -> /files/a/b) is handled by the view itself
  if (active && active.route === found && active.instance?.update) {
    const keep = active.instance.update({ path, query, match });
    if (keep !== false) {
      active.path = path;
      onChange(active);
      return;
    }
  }
  if (active) {
    try { active.instance?.destroy?.(); } catch (err) { console.error(err); }
    active.el.remove();
  }
  const el = h('section', { class: `view view-enter${found.fill ? ' fill' : ''}`, 'aria-label': found.title });
  host.append(el);
  active = { route: found, el, path, instance: null, tab: found.tab };
  try {
    active.instance = found.view(el, { path, query, match }) || {};
  } catch (err) {
    console.error(err);
    el.append(h('div', { class: 'notice danger' }, 'This screen failed to load. Pull down or reopen the app.'));
  }
  el.addEventListener('animationend', () => el.classList.remove('view-enter'), { once: true });
  onChange(active);
}
