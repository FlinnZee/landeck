import { api } from '../api.js';
import { CoreBars, Ring, Spark } from '../charts.js';
import { navigate } from '../router.js';
import { state } from '../store.js';
import { disabledCard, fmtBytes, fmtDuration, fmtRate, h, icon, segmented, skeletonBlock, swap, tween } from '../ui.js';
import { conn } from '../ws.js';

function gauge(label, sub) {
  const canvas = h('canvas', { class: 'ring-canvas', 'aria-hidden': 'true' });
  const value = h('span', { class: 'num' }, '0');
  const subEl = h('div', { class: 'gauge-sub num' }, sub || '');
  const el = h('div', { class: 'gauge', role: 'img', 'aria-label': label },
    canvas,
    h('div', { class: 'gauge-center' }, h('div', { class: 'gauge-value' }, value, h('small', null, '%')), h('div', { class: 'gauge-label' }, label)),
    subEl);
  const ring = new Ring(canvas);
  return {
    el, ring,
    set(pct, subText) {
      ring.set(pct);
      tween(value, pct, (v) => Math.round(v));
      if (subText != null) subEl.textContent = subText;
      el.setAttribute('aria-label', `${label} ${Math.round(pct)} percent`);
    },
  };
}

function metric(label, ico) {
  const v = h('div', { class: 'metric-value num' }, '–');
  return { el: h('div', { class: 'metric' }, h('div', { class: 'metric-label' }, icon(ico), label), v), v };
}

export default function dashboard(root) {
  if (state.features.dashboard === false) {
    root.append(h('div', { class: 'wrap' }, disabledCard('dashboard', 'The dashboard')));
    return {};
  }
  const wrap = h('div', { class: 'wrap dash' });
  root.append(wrap);

  const cpu = gauge('CPU');
  const ram = gauge('Memory');
  const third = gauge('GPU');
  const gauges = h('div', { class: 'card gauges' }, cpu.el, ram.el, third.el);

  const cpuSpark = h('canvas', { class: 'spark', 'aria-hidden': 'true' });
  const coreCanvas = h('canvas', { class: 'cores', 'aria-hidden': 'true' });
  const cpuNow = h('span', { class: 'num' }, '–');
  const cpuCard = h('div', { class: 'card' },
    h('div', { class: 'card-head' }, icon('cpu'), h('div', { class: 'h3' }, 'Processor'), h('div', { class: 'dim small num' }, cpuNow, '%')),
    cpuSpark,
    h('div', { class: 'tiny faint', style: { margin: '10px 0 6px' } }, 'Per core'),
    coreCanvas);

  const up = metric('Up', 'arrow-up');
  const down = metric('Down', 'arrow-down');
  const netSpark = h('canvas', { class: 'spark', 'aria-hidden': 'true' });
  const netCard = h('div', { class: 'card' },
    h('div', { class: 'card-head' }, icon('wifi'), h('div', { class: 'h3' }, 'Network')),
    h('div', { class: 'metrics' }, down.el, up.el), netSpark);

  const rd = metric('Read', 'arrow-down');
  const wr = metric('Write', 'arrow-up');
  const ioSpark = h('canvas', { class: 'spark short', 'aria-hidden': 'true' });
  const ioCard = h('div', { class: 'card' },
    h('div', { class: 'card-head' }, icon('drive'), h('div', { class: 'h3' }, 'Disk activity')),
    h('div', { class: 'metrics' }, rd.el, wr.el), ioSpark);

  const drives = h('div', { class: 'stack', style: { gap: '14px' } });
  const drivesCard = h('div', { class: 'card' }, h('div', { class: 'card-head' }, icon('drive'), h('div', { class: 'h3' }, 'Drives')), drives);

  const battery = h('div', { class: 'card', hidden: true });

  let topMode = 'cpu';
  const topList = h('div', { class: 'top-list' });
  const topCard = h('div', { class: 'card' },
    h('div', { class: 'card-head' }, icon('activity'), h('div', { class: 'h3' }, 'Top processes'),
      h('button', { class: 'btn small ghost', onclick: () => navigate('/processes') }, 'All', icon('chevron-right'))),
    segmented([{ value: 'cpu', label: 'CPU' }, { value: 'mem', label: 'Memory' }], 'cpu', (v) => { topMode = v; if (last) renderTop(last); }, { label: 'Sort top processes' }),
    topList);

  const info = h('div', { class: 'kv' });
  const infoCard = h('div', { class: 'card' }, h('div', { class: 'card-head' }, icon('monitor'), h('div', { class: 'h3' }, 'This PC')), info);
  const windowLine = h('div', { class: 'window-line ellipsis' });

  const skeleton = h('div', { class: 'stack' }, skeletonBlock(170), skeletonBlock(150), skeletonBlock(120));
  const live = h('div', { class: 'stack', hidden: true },
    gauges, windowLine,
    h('div', { class: 'grid-auto stack' }, cpuCard, netCard, ioCard, drivesCard, battery, topCard, infoCard));
  wrap.append(skeleton, live);

  const sparkCpu = new Spark(cpuSpark, { max: 100 });
  const cores = new CoreBars(coreCanvas);
  const sparkNet = new Spark(netSpark, { series: [{ color: '--accent', fill: true }, { color: '--text-faint', fill: false }] });
  const sparkIo = new Spark(ioSpark, { series: [{ color: '--ok', fill: true }, { color: '--warn', fill: false }] });

  let last = null;
  let sysInfo = null;

  function renderTop(s) {
    const rows = (s.top?.[topMode] || []);
    swap(topList, rows.length ? rows.map((p) => h('div', { class: 'top-row' },
      h('div', { class: 'ellipsis' }, p.name.replace(/\.exe$/i, '')),
      h('div', { class: 'num dim small' }, topMode === 'cpu' ? `${p.cpu.toFixed(1)}%` : fmtBytes(p.mem)))) : h('div', { class: 'faint small' }, 'Gathering\u2026'));
  }

  function renderDrives(list) {
    swap(drives, list.map((d) => {
      const level = d.percent > 90 ? 'danger' : d.percent > 80 ? 'warn' : '';
      return h('div', { class: 'drive' },
        h('div', { class: 'row', style: { justifyContent: 'space-between' } },
          h('b', null, d.mount), h('span', { class: 'small dim num' }, `${fmtBytes(d.free)} free of ${fmtBytes(d.total)}`)),
        h('div', { class: `bar ${level}` }, h('i', { style: { width: `${d.percent}%` } })));
    }));
  }

  function renderBattery(b) {
    battery.hidden = !b;
    if (!b) return;
    swap(battery,
      h('div', { class: 'card-head' }, icon(b.plugged ? 'battery-charging' : 'battery'), h('div', { class: 'h3' }, 'Battery'),
        h('div', { class: 'small dim' }, b.plugged ? 'Plugged in' : b.secs_left ? `${fmtDuration(b.secs_left)} left` : 'On battery')),
      h('div', { class: `bar ${b.percent < 15 ? 'danger' : b.percent < 30 ? 'warn' : ''}` }, h('i', { style: { width: `${b.percent}%` } })),
      h('div', { class: 'num', style: { marginTop: '8px', fontWeight: 600 } }, `${Math.round(b.percent)}%`));
  }

  function renderInfo(s) {
    if (!sysInfo) return;
    const rows = [
      ['User', sysInfo.user],
      ['System', `${sysInfo.os.name} ${sysInfo.os.release || ''}`.trim()],
      ['Build', sysInfo.os.build],
      ['CPU', sysInfo.cpu.model],
      ['Uptime', fmtDuration(s?.uptime)],
      ['Address', (sysInfo.ips || []).join(', ') || '–'],
    ];
    swap(info, rows.map(([k, v]) => h('div', { class: 'kv-row' }, h('span', { class: 'faint' }, k), h('span', { class: 'ellipsis' }, v || '–'))));
  }

  function onStats(s) {
    last = s;
    skeleton.hidden = true;
    live.hidden = false;
    cpu.set(s.cpu.total, `${s.cpu.cores.length} threads`);
    ram.set(s.mem.percent, `${fmtBytes(s.mem.used)} / ${fmtBytes(s.mem.total)}`);
    const gpu = s.gpu?.[0];
    if (gpu) {
      third.el.querySelector('.gauge-label').textContent = 'GPU';
      third.set(gpu.util || 0, `${gpu.temp != null ? `${Math.round(gpu.temp)}\u00b0C \u00b7 ` : ''}${fmtBytes((gpu.mem_used || 0) * 1048576)}`);
    } else {
      const sys = s.disks?.find((d) => /^C:/i.test(d.mount)) || s.disks?.[0];
      third.el.querySelector('.gauge-label').textContent = sys ? `Disk ${sys.mount}` : 'Swap';
      third.set(sys ? sys.percent : s.swap.percent, sys ? `${fmtBytes(sys.free)} free` : fmtBytes(s.swap.used));
    }
    tween(cpuNow, s.cpu.total, (v) => v.toFixed(0));
    sparkCpu.push(s.cpu.total);
    cores.set(s.cpu.cores);
    down.v.textContent = fmtRate(s.net.down);
    up.v.textContent = fmtRate(s.net.up);
    sparkNet.push(s.net.down, s.net.up);
    rd.v.textContent = fmtRate(s.disk_io.read);
    wr.v.textContent = fmtRate(s.disk_io.write);
    sparkIo.push(s.disk_io.read, s.disk_io.write);
    renderDrives(s.disks || []);
    renderBattery(s.battery);
    renderTop(s);
    windowLine.hidden = !s.window;
    swap(windowLine, s.window ? [icon('layout'), h('span', { class: 'ellipsis' }, s.window)] : []);
    renderInfo(s);
  }

  async function loadInfo() {
    try {
      sysInfo = await api.get('/system/info');
      renderInfo(last);
    } catch { /* stats keep flowing anyway */ }
  }

  const offStats = conn.on('stats', onStats);
  const unsub = conn.sub('stats');
  loadInfo();
  api.get('/system/stats').then((s) => { if (!last) onStats(s); }).catch(() => {});

  return {
    refresh() { loadInfo(); },
    destroy() {
      offStats();
      unsub();
      [cpu.ring, ram.ring, third.ring, sparkCpu, cores, sparkNet, sparkIo].forEach((x) => x.destroy());
    },
  };
}
