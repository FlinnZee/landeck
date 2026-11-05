// Small canvas renderers for gauges and sparklines. No libraries.

function cssVar(name, el = document.documentElement) {
  return getComputedStyle(el).getPropertyValue(name).trim();
}

function fit(canvas) {
  const dpr = Math.min(window.devicePixelRatio || 1, 3);
  const { width, height } = canvas.getBoundingClientRect();
  const w = Math.max(1, Math.round(width * dpr));
  const hgt = Math.max(1, Math.round(height * dpr));
  if (canvas.width !== w || canvas.height !== hgt) {
    canvas.width = w;
    canvas.height = hgt;
  }
  return { ctx: canvas.getContext('2d'), w, h: hgt, dpr };
}

function reducedMotion() {
  return document.documentElement.dataset.motion === 'off' || matchMedia('(prefers-reduced-motion: reduce)').matches;
}

export class Ring {
  constructor(canvas, { color = '--accent', thickness = 0.11 } = {}) {
    this.canvas = canvas;
    this.color = color;
    this.thickness = thickness;
    this.value = 0;
    this.shown = 0;
    this.raf = 0;
    this.ro = new ResizeObserver(() => this.draw());
    this.ro.observe(canvas);
  }

  set(value) {
    this.value = Math.max(0, Math.min(100, value || 0));
    if (reducedMotion()) {
      this.shown = this.value;
      this.draw();
      return;
    }
    cancelAnimationFrame(this.raf);
    const from = this.shown;
    const t0 = performance.now();
    const step = (t) => {
      const k = Math.min(1, (t - t0) / 600);
      this.shown = from + (this.value - from) * (1 - Math.pow(1 - k, 3));
      this.draw();
      if (k < 1) this.raf = requestAnimationFrame(step);
    };
    this.raf = requestAnimationFrame(step);
  }

  draw() {
    const { ctx, w, h } = fit(this.canvas);
    const size = Math.min(w, h);
    if (size < 12) return; // not laid out yet
    const lw = size * this.thickness;
    const r = size / 2 - lw * 1.15; // leave room for the glow
    const cx = w / 2;
    const cy = h / 2;
    const start = Math.PI * 0.75;
    const sweep = Math.PI * 1.5;
    ctx.clearRect(0, 0, w, h);
    ctx.lineCap = 'round';
    ctx.lineWidth = lw;
    ctx.strokeStyle = cssVar('--surface-3');
    ctx.beginPath();
    ctx.arc(cx, cy, r, start, start + sweep);
    ctx.stroke();
    const v = this.shown / 100;
    if (v <= 0.002) return;
    const hot = this.shown > 90 ? cssVar('--danger') : this.shown > 75 ? cssVar('--warn') : null;
    const color = hot || cssVar(this.color);
    ctx.strokeStyle = color;
    ctx.shadowColor = color;
    ctx.shadowBlur = lw * 0.9;
    ctx.beginPath();
    ctx.arc(cx, cy, r, start, start + sweep * v);
    ctx.stroke();
    ctx.shadowBlur = 0;
  }

  destroy() {
    cancelAnimationFrame(this.raf);
    this.ro.disconnect();
  }
}

export class Spark {
  // series: [{ color: '--accent', fill: true }]
  constructor(canvas, { series = [{ color: '--accent', fill: true }], points = 60, max = null } = {}) {
    this.canvas = canvas;
    this.series = series;
    this.points = points;
    this.fixedMax = max;
    this.data = series.map(() => []);
    this.ro = new ResizeObserver(() => this.draw());
    this.ro.observe(canvas);
  }

  push(...values) {
    values.forEach((v, i) => {
      const arr = this.data[i];
      arr.push(Math.max(0, v || 0));
      if (arr.length > this.points) arr.shift();
    });
    this.draw();
  }

  draw() {
    const { ctx, w, h, dpr } = fit(this.canvas);
    ctx.clearRect(0, 0, w, h);
    const max = this.fixedMax ?? Math.max(1, ...this.data.flat()) * 1.15;
    const pad = 3 * dpr;
    const stepX = (w - pad * 2) / (this.points - 1);

    // faint baseline grid
    ctx.strokeStyle = cssVar('--border');
    ctx.lineWidth = dpr;
    for (let g = 1; g < 4; g++) {
      const y = Math.round((h / 4) * g) + 0.5;
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(w, y);
      ctx.stroke();
    }

    this.series.forEach((s, i) => {
      const arr = this.data[i];
      if (arr.length < 2) return;
      const color = cssVar(s.color);
      const offset = this.points - arr.length;
      const pts = arr.map((v, k) => [pad + (offset + k) * stepX, h - pad - (v / max) * (h - pad * 2)]);
      ctx.beginPath();
      ctx.moveTo(pts[0][0], pts[0][1]);
      for (let k = 1; k < pts.length; k++) {
        const [x0, y0] = pts[k - 1];
        const [x1, y1] = pts[k];
        const mx = (x0 + x1) / 2;
        ctx.bezierCurveTo(mx, y0, mx, y1, x1, y1);
      }
      ctx.lineWidth = 2 * dpr;
      ctx.strokeStyle = color;
      ctx.lineJoin = 'round';
      ctx.stroke();
      if (s.fill) {
        ctx.lineTo(pts[pts.length - 1][0], h);
        ctx.lineTo(pts[0][0], h);
        ctx.closePath();
        const grad = ctx.createLinearGradient(0, 0, 0, h);
        grad.addColorStop(0, hexA(color, 0.28));
        grad.addColorStop(1, hexA(color, 0));
        ctx.fillStyle = grad;
        ctx.fill();
      }
      const [lx, ly] = pts[pts.length - 1];
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(lx, ly, 2.6 * dpr, 0, Math.PI * 2);
      ctx.fill();
    });
  }

  destroy() {
    this.ro.disconnect();
  }
}

function hexA(color, a) {
  if (color.startsWith('#') && (color.length === 7 || color.length === 4)) {
    const full = color.length === 4 ? `#${color[1]}${color[1]}${color[2]}${color[2]}${color[3]}${color[3]}` : color;
    const n = parseInt(full.slice(1), 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
  }
  return color;
}

export class CoreBars {
  constructor(canvas) {
    this.canvas = canvas;
    this.values = [];
    this.ro = new ResizeObserver(() => this.draw());
    this.ro.observe(canvas);
  }

  set(values) {
    this.values = values || [];
    this.draw();
  }

  draw() {
    const { ctx, w, h, dpr } = fit(this.canvas);
    ctx.clearRect(0, 0, w, h);
    const n = this.values.length;
    if (!n) return;
    const gap = 3 * dpr;
    const bw = Math.max(2 * dpr, (w - gap * (n - 1)) / n);
    const track = cssVar('--surface-3');
    const accent = cssVar('--accent');
    const warn = cssVar('--warn');
    const danger = cssVar('--danger');
    this.values.forEach((v, i) => {
      const x = i * (bw + gap);
      ctx.fillStyle = track;
      roundRect(ctx, x, 0, bw, h, Math.min(bw / 2, 3 * dpr));
      ctx.fill();
      const bh = Math.max(2 * dpr, (v / 100) * h);
      ctx.fillStyle = v > 90 ? danger : v > 75 ? warn : accent;
      roundRect(ctx, x, h - bh, bw, bh, Math.min(bw / 2, 3 * dpr));
      ctx.fill();
    });
  }

  destroy() {
    this.ro.disconnect();
  }
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  if (ctx.roundRect) ctx.roundRect(x, y, w, h, r);
  else ctx.rect(x, y, w, h);
}
