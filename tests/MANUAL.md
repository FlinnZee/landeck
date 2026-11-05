# Manual checks

Things the automated tests can't do because they need a real phone, a real reboot or a human on the PC.
Run through this before tagging a release.

## Pairing
- [ ] Tray → Pair new device shows a QR and a 6-digit code with a 2-minute countdown.
- [ ] Scan with the iPhone camera, accept the certificate warning (Show Details → visit this website).
- [ ] Name the device, submit. The PC shows a topmost Yes/No box. "No" refuses, "Yes" pairs.
- [ ] Typing the 6-digit code on `https://<pc-ip>:8765` works the same way.
- [ ] Reusing the same code fails; waiting 2 minutes expires it.
- [ ] Optional: install `/ca.mobileconfig`, trust it, reload — the warning is gone and Add to Home Screen opens full screen.

## Daily use
- [ ] Dashboard gauges and sparklines move every second; CPU stress (>90% for 30 s) raises an alert toast.
- [ ] Copy text on the PC → toast on the phone, history updates. Send text from the phone → paste on the PC.
- [ ] Send a photo to the PC clipboard, paste it into Paint.
- [ ] Upload a large video from Photos. Turn Wi-Fi off mid-upload, turn it back on: it resumes.
- [ ] Download a file, play a video from the PC (seeking works), download a folder as zip.
- [ ] Delete a file → it's in the Recycle Bin.
- [ ] Trackpad: move, tap, two-finger tap (right click), two-finger scroll, double-tap-and-drag a window.
- [ ] Keyboard: type into Notepad, including emoji; sticky Ctrl + C copies; Win+L is refused.
- [ ] Screen: live view at Balanced shows smooth motion; pinch-zoom and pan; Tap to click clicks where tapped.
- [ ] While viewing, the tray icon turns red with a dot and a toast says the screen is being viewed.
- [ ] Volume slider and mute change the Windows volume; media keys control Spotify/YouTube.
- [ ] Quick actions: Lock, Display off, Screenshot to Downloads all work.
- [ ] Kill a harmless app from Processes; `lsass.exe` is refused.

## Connection
- [ ] Put the PC to sleep from the phone: the phone shows "PC is going offline", then "Waiting for PC". Wake the PC: the phone reconnects on its own.
- [ ] Toggle the phone's Wi-Fi: amber pill, then green, no reload.
- [ ] Restart the agent from the tray: the phone reconnects within ~10 s.
- [ ] Reboot the PC and sign in: the agent starts elevated without a UAC prompt and the phone reconnects.

## Security
- [ ] Admin page (`https://localhost:8765/admin`) works on the PC and returns 403 from the phone.
- [ ] Revoke the phone in the admin page: it's kicked to the pairing screen with "This device was revoked".
- [ ] Terminal stays off until enabled in the admin page, then asks for the PIN; commands appear in the audit log.
- [ ] Audit log shows pairings, failed logins, power actions, deletes and kills.
