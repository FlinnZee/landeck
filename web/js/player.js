// Full-screen movie player. Streams video from the PC as HLS so HEVC/HDR movies and MKV/AVI
// containers play on the phone. iOS Safari plays HLS natively; other browsers use hls.js.
import { api, url } from './api.js';
import { icon } from './icons.js';
import { errorToast, fmtBytes, h, swap, toast } from './ui.js';

let hlsScript = null;

function loadHls() {
  if (window.Hls) return Promise.resolve(window.Hls);
  if (!hlsScript) {
    hlsScript = new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = '/js/vendor/hls.min.js';
      s.onload = () => resolve(window.Hls);
      s.onerror = () => reject(new Error('hls.js failed to load'));
      document.head.append(s);
    });
  }
  return hlsScript;
}

export function openPlayer(root, path, name) {
  const video = h('video', { class: 'player-video', controls: true, playsinline: true, autoplay: true,
    'webkit-playsinline': true });
  const spinner = h('div', { class: 'player-loading' }, h('div', { class: 'spinner' }), h('div', { class: 'small dim load-msg' }, 'Preparing stream…'));
  const badge = h('div', { class: 'player-badge', hidden: true });
  const title = h('div', { class: 'player-title ellipsis' }, name);
  const closeBtn = h('button', { class: 'icon-btn player-close', 'aria-label': 'Close', onclick: () => close() }, icon('x'));
  const audioBtn = h('button', { class: 'icon-btn', 'aria-label': 'Audio track', hidden: true });
  const top = h('div', { class: 'player-top' }, closeBtn, title, h('div', { class: 'spacer' }), audioBtn, badge);
  const stage = h('div', { class: 'player-stage' }, video, spinner);
  const overlay = h('div', { class: 'player', role: 'dialog', 'aria-label': `Playing ${name}` }, top, stage);
  document.body.append(overlay);
  document.body.classList.add('player-open');

  let hls = null;
  let sid = null;
  let closed = false;
  let audio = null;

  const onKey = (e) => { if (e.key === 'Escape') close(); };
  document.addEventListener('keydown', onKey);

  async function close() {
    if (closed) return;
    closed = true;
    document.removeEventListener('keydown', onKey);
    try { video.pause(); } catch { /* */ }
    if (hls) { try { hls.destroy(); } catch { /* */ } }
    if (sid) api.del(`/stream/${sid}`).catch(() => {});
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    overlay.remove();
    document.body.classList.remove('player-open');
  }

  function fail(msg) {
    swap(spinner, h('div', { class: 'empty', style: { color: 'var(--text)' } },
      h('div', { class: 'empty-icon' }, icon('alert')),
      h('p', null, msg),
      h('div', { class: 'row' },
        h('a', { class: 'btn small', href: `${url(fileUrl())}?dl=1`, download: name }, icon('download'), 'Download'),
        h('button', { class: 'btn small ghost', onclick: close }, 'Close'))));
    spinner.hidden = false;
  }

  function fileUrl() {
    // direct (non-streamed) URL for the download fallback
    return `/files/stat?root=${encodeURIComponent(root)}&path=${encodeURIComponent(path)}`;
  }

  async function play(opts = {}) {
    try {
      const info = await api.get(`/stream/info?root=${encodeURIComponent(root)}&path=${encodeURIComponent(path)}`);
      if (!info.available) { fail('ffmpeg isn’t installed on the PC, so video can’t be streamed. Install it, or use Download / Open on PC.'); return; }
      spinner.querySelector('.load-msg').textContent = info.direct_play ? 'Starting…' : 'Converting on the PC…';
      const open = await api.post('/stream/open', { root, path, audio: opts.audio ?? audio, transcode: opts.transcode });
      sid = open.session;
      audio = opts.audio ?? audio;
      paintBadge(open, info);
      setupAudio(open);
      const src = url(open.m3u8);
      const canNative = video.canPlayType('application/vnd.apple.mpegurl');
      const ua = navigator.userAgent;
      // iPhone/iPad Safari: native HLS gives the hardware HEVC/HDR path. Everyone else: hls.js.
      const appleSafari = /safari/i.test(ua) && !/chrome|crios|fxios|android|edg/i.test(ua);
      if (appleSafari && canNative) {
        video.src = src;
      } else {
        const Hls = await loadHls().catch(() => null);
        if (Hls && Hls.isSupported()) {
          hls = new Hls({ maxBufferLength: 30, enableWorker: true });
          hls.loadSource(src);
          hls.attachMedia(video);
          hls.on(Hls.Events.ERROR, (_e, d) => { if (d.fatal) fail('Playback error while streaming. Try Download or Open on PC.'); });
        } else if (canNative) {
          video.src = src;
        } else {
          fail('This browser can’t play the video stream.');
          return;
        }
      }
      video.play().catch(() => {});
    } catch (err) {
      if (err.status === 0) fail('Lost the connection to the PC.');
      else fail(err.detail || 'Couldn’t start the stream.');
    }
  }

  function paintBadge(open, info) {
    const v = open.video || info;
    const hdr = v.hdr ? ' · HDR' : '';
    const q = `${v.height ? v.height + 'p' : ''}${v.codec ? ' ' + v.codec.toUpperCase() : ''}`;
    swap(badge, open.transcode
      ? h('span', { class: 'warn' }, icon('zap'), `Converting${v.hdr ? ' · HDR→SDR' : ''}`)
      : h('span', null, icon('check'), `Original ${q}${hdr}`.trim()));
    badge.hidden = false;
  }

  function setupAudio(open) {
    const tracks = open.audios || [];
    audioBtn.hidden = tracks.length < 2;
    if (tracks.length < 2) return;
    audioBtn.innerHTML = '';
    audioBtn.append(icon('volume'));
    audioBtn.onclick = async () => {
      const { sheet } = await import('./ui.js');
      const row = (t, i) => {
        const label = `${t.lang || 'Track ' + (i + 1)} · ${t.codec.toUpperCase()}${t.channels ? ' ' + t.channels + 'ch' : ''}`;
        const btn = h('button', { class: 'item', onclick: () => {
          s.close();
          const was = video.currentTime;
          restart({ audio: t.index }).then(() => { video.currentTime = was; });
        } },
        h('div', { class: 'item-icon' }, icon('volume')),
        h('div', { class: 'item-body' }, h('div', { class: 'item-title' }, label), t.title && h('div', { class: 'item-sub' }, t.title)));
        return btn;
      };
      const s = sheet({ title: 'Audio track', body: h('div', { class: 'list' }, tracks.map(row)) });
    };
  }

  async function restart(opts) {
    if (hls) { try { hls.destroy(); } catch { /* */ } hls = null; }
    if (sid) { api.del(`/stream/${sid}`).catch(() => {}); sid = null; }
    spinner.hidden = false;
    await play(opts);
  }

  video.addEventListener('loadeddata', () => { spinner.hidden = true; });
  video.addEventListener('playing', () => { spinner.hidden = true; });
  video.addEventListener('error', () => { if (!closed && !hls && !video.canPlayType('application/vnd.apple.mpegurl')) fail('This video couldn’t play.'); });

  play();
  void toast;
  void fmtBytes;
  return { close };
}
