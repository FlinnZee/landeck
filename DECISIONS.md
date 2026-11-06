# Decisions

Small calls made while building, one line each, so future me knows why.

- Repo root is the project folder itself (no extra `landeck/` level); `agent/`, `web/`, `tests/`, `assets/` sit at the top.
- Self-signed cert replaced by a tiny local CA that signs the server cert, so `/ca.mobileconfig` can remove the Safari warning; the CA carries critical Name Constraints (`.local`, the hostname, private IP ranges) so trusting it can't be abused for real websites.
- Server cert lifetime is 800 days (iOS refuses longer for custom CAs) and is re-issued automatically when the LAN IP set changes; the running SSL context reloads it without a restart.
- Device tokens are 256-bit `token_urlsafe(32)`; only their SHA-256 is stored, compared with `hmac.compare_digest`.
- Rate limit: 5 failures per IP per minute blocks that IP for 5 minutes; the 6th try gets 429 even with a valid token.
- Pairing accepts either the URL secret (QR) or the 6-digit code; any match burns the session, expired or not.
- The PC prompt uses `MessageBoxTimeoutW` with "No" as the default button and a 60 s timeout (timeout = No).
- Admin API also checks the Host header (DNS rebinding) and requires `X-LanDeck: 1` on writes, on top of loopback-only.
- `<img>`, `<video>` and download links can't send a bearer header, so file links are short-lived HMAC-signed URLs bound to the device; revoking the device kills its links.
- Files served from the PC get `Content-Security-Policy: sandbox` and HTML/SVG/script types are downgraded to `text/plain`, so a malicious file can't run script on the app's origin.
- Path checks: reject `..`, `:` (alternate data streams) and control chars, then `realpath` + `commonpath` against the root, which also catches junctions; zips and search skip symlinks and junctions.
- Uploads go to `Downloads\LanDeck` by default, or into the folder being browsed; resume works from the server's byte offset, not chunk boundaries.
- Opening an executable file on the PC from the phone needs `confirm: true` (hold to confirm in the UI).
- Quick actions got a `builtin` type (volume, mute toggle, screenshot, display off) next to the spec's types, because those aren't programs.
- Night Light can't be toggled through a public API, so the default action opens `ms-settings:nightlight` instead.
- Sleep on Modern Standby machines (no S3) turns the display off, which is how those machines enter standby.
- `shutdown`/`restart` schedules use Windows' own `shutdown /t` timer so they survive an agent crash; others use an in-process timer.
- Trackpad moves are applied as "current cursor + delta" absolute moves, so Windows pointer acceleration doesn't stack on the phone's curve.
- Win+L is blocked in the input module (the phone can't unlock afterwards); Lock lives in Power instead.
- Screen frames are JPEG over the socket with a 20-byte header; the server keeps at most two frames in flight and skips capture until the phone acks.
- The mouse cursor is drawn into frames because the capture library doesn't include it.
- Clipboard changes come from `AddClipboardFormatListener` on a hidden top-level window (message-only windows miss power broadcasts); polling the sequence number is the fallback.
- Secrets in clipboard history are detected by keywords, known token shapes and an entropy check, and masked until tapped.
- Terminal runs each command as a fresh `powershell.exe -EncodedCommand` (no PTY); the working folder is carried between commands with a marker line.
- Events addressed to one device (terminal output) skip topic subscriptions; everything else needs a subscription or is a broadcast topic.
- The "System Idle Process" is hidden from the process list; it's idle time, not a process.
- Ports: 8765, then the next free one up to +19, saved to config.
- Firewall rules are added for private and domain networks only; the admin page warns when the network is marked Public.
- The startup task points at `pythonw.exe -m agent.main` when running from source, at the exe when packaged.
- Dev mode (`--dev`) uses its own mutex name so it can run next to an installed agent (on the next free port).
- Service worker is network-first for the shell and never touches `/api`, `/ws` or `/admin`; registration failures are ignored.
- Icons are hand-drawn Lucide-style paths bundled in `web/js/icons.js`; no icon font, no CDN.
- Tests run with `LANDECK_DRY_RUN=1`: power, input, kill, file delete and toasts are recorded instead of executed.

## v1.1

- Phones now prefer `https://landeck.local` over the raw IP everywhere (pairing QR default, first connection candidate),
  because iOS handles a self-signed cert on an mDNS `.local` name far better than on a raw private IP. The IP stays as a
  fallback the parallel probe can still pick.
- Connection probing is parallel, not sequential, so a dead/old origin (e.g. a Home-Screen app pinned to a past IP) no
  longer delays finding a live address.
- `origin_ok` now accepts any private-LAN IP on our port, not just the current one, so a phone added to the Home Screen
  on one IP keeps working after the PC's IP changes. Bearer token + the custom header already make the API CSRF-immune.
- mDNS is re-advertised automatically if the registration ever drops, since `landeck.local` is now the main way in.
- RAM cleaner calls `NtSetSystemInformation` directly via ctypes (no native code), the same mechanism as Mem Reduct.
  SeProfileSingleProcessPrivilege is LUID 13 (29 is SeImpersonate) — getting that wrong silently fails the purges.
- Default clean set: working sets, standby + low-priority standby, system file cache, combine memory (win10+),
  registry cache (win8.1+). It reports freed bytes as the rise in available memory.
- Wake display nudges the mouse one pixel and sets ES_DISPLAY_REQUIRED; the SC_MONITORPOWER "on" message alone is
  unreliable from a background process, the input event is what actually wakes the screen.

## v1.1.1

- The shell HTML (`/`, `/pair`, `/admin`) is served `no-store` with a `?v=<version>` stamp on every css/js link, so a
  browser can never reuse a stale stylesheet or script after an update. A phone that cached a broken `base.css` (serif
  font, giant icons) self-heals on the next load. The service worker falls back to cache with `ignoreSearch` so the
  version query doesn't break offline.

## v1.1.2

- Files now lists every mounted drive (C:, other disks, USB, SD cards, network drives) as a root, enumerated live via
  `psutil.disk_partitions` + `GetDriveType`/`GetVolumeInformation`, so plugging in a USB stick makes it appear. A
  `show_drives` admin toggle (default on) can hide them; the per-drive root still enforces the same realpath/commonpath
  and junction checks, so `..` can't escape the drive.

## v1.2.0

- Video streaming so movies play on the phone. HLS with fragmented-MP4 segments, generated on demand and aligned to
  keyframes, so seeking works without pre-processing the whole file. H.264/HEVC video is stream-copied (full quality,
  HDR10 preserved, hvc1 tag for Apple); audio is copied when AAC, else converted to AAC stereo. Unsupported video
  codecs transcode via NVENC (h264, SDR, capped 1080p) as a fallback.
- Stream URLs carry a per-session capability token (?k=), because iOS's native HLS player can't send our auth header,
  exactly like the file download links. Playlist/init/segment endpoints validate that token instead of the bearer.
- The phone uses native HLS on iOS Safari (hardware HEVC/HDR path) and hls.js everywhere else; hls.js is vendored at
  web/js/vendor/hls.min.js (MIT) to stay offline/no-CDN. CSP gains worker-src blob: for hls.js's worker.
- ffmpeg/ffprobe are discovered on PATH, in the winget package dirs, or from a configured ffmpeg_path; the feature
  reports unavailable (with install hint) when missing instead of failing mid-playback.
- Lesson: validate web modules with `node --input-type=module --check`; plain `node --check` parses as a script and
  missed an extra ")" that the browser's module parser rejected.
