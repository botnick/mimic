# What was tested, and where the edges are

This is the honest status page. Everything below was run on hardware, not reasoned about
in the abstract. If something is shaky or flat-out doesn't work, it's here too.

## Test device

| | |
|---|---|
| Model | iPhone 8 (iPhone10,1, A11, arm64e) |
| iOS | 16.7.16 |
| Jailbreak | palera1n, **rootless** (`/var/jb`) |
| Passcode | none (required — A11 + passcode + this jailbreak risks an SEP bootloop) |
| Link | USB, straight into the Mac (a hub drops the device) |
| Host | macOS, stock `python3`, Homebrew `iproxy` / `ldid` |
| frida | server 16.1.4 (arm64e, signed); matching client |
| go-ios | v1.2.0 |

## Works, and validated end to end

- **Screen reading** (`mimic_look`). Returns labeled, tappable elements with coordinates.
  A full Settings screen comes back as ~45 elements.
- **Tap** through accessibility. Confirmed on real buttons (`UIButton` via
  `sendActionsForControlEvents:`), navigation cells, and table/collection rows.
- **Type** into text fields, including the recipient and body fields in Messages.
- **Swipe / scroll** on the home screen and standard lists.
- **Launch / list / search apps**, **screenshot**, **home / wake / unlock**,
  **frontmost app**, **close app**.
- **Send a text message** end to end: launch Messages → tap Compose → type the recipient
  → type the body → tap Send → confirm the sent bubble. Done entirely through the tools.
- **Screen video** (`mimic_record`). Frames pulled from the render server, encoded on the
  device, assembled into an mp4. Verified producing real motion.
- **Call with TTS to the callee** (`mimic_call`). Validated live across several numbers:
  the callee answers, hears the spoken message over a normal cellular call, and the call
  hangs up. The voice is whatever system voice is installed for the language (Thai ships
  only "Kanya"); the `pitch` option can deepen or raise it.
- **SSL pinning bypass** (`mimic_ssl`). Toggled on/off and read back; the write lands in
  the file the tweak actually reads. With it on, a pinned app's HTTPS was decrypted
  through a proxy and its API calls were readable.
- **Operating a hardened, anti-frida app.** With the gadget bypass installed, a commercial
  app that rejects a frida attach was opened, navigated, and typed into.
- **Per-app SSL unpinning** (`mimic_unpin`). Hooks the foreground app's BoringSSL
  custom-verify (`SSL_set_custom_verify` / `SSL_CTX_set_custom_verify`) and `SecTrust*` —
  confirmed installing all four hooks in Safari with no errors. Complements the system tweak
  for apps that pin in ways it misses.
- **Built-in MITM** (`mimic_mitm`). `start` launches `mitmweb` on the host with an easy web
  password (`Aa1234`), binds the proxy + web-UI ports, and flips the SSL bypass on —
  confirmed the proxy and password-protected web UI come up and `status` tracks the process.
  (A stale process on the port is cleared first so the relaunch binds cleanly.)
- **Hardware buttons** (`mimic_button`). Volume up/down (the on-screen HUD appears), mute,
  and power/lock (a short press locks the screen — confirmed via `isUILocked` 0→1→0). These
  use Consumer-HID keyboard usages, a different path from the digitizer taps below.
- **High-fps capture** for the live viewer. A CARenderServer frame (`device.frida_frame`)
  pulled over frida renders the composited display, sustained ~25–40 fps (200 frames straight
  with no leak after the autorelease-pool fix; see Gotchas) — much smoother than go-ios' ~9.
  It does **not** bypass DRM: it reads the same capture composite, so the secure flag still
  blacks out FairPlay video and screenshot-protected apps (verified on buydrm.com — Safari's
  UI captures, only the FairPlay video is excluded).
- **go-ios extras**: device info / battery / process list, GPS spoof, packet capture
  (`.pcap`) and syslog, app install/uninstall, app-container file pull — the reads plus GPS,
  pcap and syslog confirmed on hardware.
- **More go-ios USB tools** (added later) — each one exercised on the iPhone 8 over USB and
  checked for a real effect, not just a zero exit code:
  - `mimic_diag` — disk space + MobileGestalt (serial/model) + IORegistry diagnostics, real data.
  - `mimic_crash` — listed 232 reports **and** pulled all 232 to disk.
  - `mimic_monitor` — sysmontap CPU load, ~45% over 6 samples. (go-ios streams it on stderr, so
    the wrapper captures to a file instead of blocking on a read.)
  - `mimic_lang` — read en-TH / locale. `mimic_kill` — killed a running app by bundle id.
    `mimic_ps` / `mimic_battery` / `mimic_info` — real data.
  - `mimic_devicestate` — `enable` then `list` shows the profile **IsActive=true**
    (`SlowNetworkCondition / SlowNetwork3GAverage`); `reset` clears it back to none. Verified the
    condition actually engages, not just that the call returns.
  - `mimic_forward` — forwarded host:12222 → device:44 and read the device's real SSH banner
    (`SSH-2.0-dropbear_2022.83`) back through the tunnel, so it genuinely moves bytes.
  - `mimic_location gpx=` — starts a moving-route session via the same `DTSimulateLocation`
    developer-image path as the point spoof above (which is confirmed); the route start returns ok.
  - `mimic_profile` — `list` / `remove` work. `add` only **stages** a profile over lockdown:
    this device isn't supervised, so iOS then needs the user to approve it on-device, and a
    global-proxy payload is rejected outright (so there's no silent system-proxy install here).
  These ride lockdown/instruments over USB, so they work even on an app that rejects a frida
  attach. (A `mimic_webjs` WebInspector wrapper was prototyped but **removed**: it needs
  Settings → Safari → Advanced → Web Inspector toggled on, so it didn't work out of the box.)
- **The live viewer** (`python3 -m mimic.ios.viewer`). Mirrors the device; a click maps to
  the nearest a11y element (home-screen icons launch their app), a drag streams a real finger
  (digitizer down→move→up) so scrolling follows the cursor, and the rail fires hardware buttons.
- **The one-command installer** (`setup.sh`). Brought the stack up and registered the MCP;
  re-runs cleanly.

## Works, with a caveat

- **Scrolling inside some apps.** The swipe is a synthetic pan. It scrolls the home screen
  and most table views, but some in-app scroll views ignore it (the same gesture-recognizer
  limitation that kills synthetic taps). When it matters, accessibility helps: because
  `mimic_tap` fires by label, you can often act on an element that is below the fold
  without scrolling to it at all.
- **Custom-drawn controls.** A view that is not a `UIControl` and has no useful
  accessibility action may not respond to a tap. The calculator keypad is the standard
  example — it draws its own buttons and `accessibilityActivate()` isn't enough. This is a
  limit of the approach, not a bug to file.
- **Setting a Wi-Fi proxy from the tools.** Doable but fiddly — the per-network detail page
  buries the proxy section, and the "more info" buttons are awkward to disambiguate by
  label. For a debugging session it's faster to set the proxy by hand on the device, or
  over SSH.

## Doesn't work — and why, so you don't retry it

These were each chased down properly. The point of writing them up is so the next person
doesn't burn a day rediscovering them.

- **Discrete taps via raw `IOHIDEvent`.** Pans and scrolls dispatch fine; discrete taps
  never fire the tap gesture recognizers. Tried from SpringBoard injection and from an
  entitled, cross-compiled touch daemon — same result every time. This is why taps go
  through accessibility instead. Two more variants were checked against SimulateTouch's
  approach: `frida attach backboardd` (where SimulateTouch injects its Substrate dylib) is
  **refused** by the process, and `IOHIDEventCreateDigitizerFingerEventWithQuality`
  dispatched from SpringBoard still doesn't launch an icon. Real discrete taps need a
  persistent backboardd tweak — not reachable from frida. (Drags/scrolls do work, so the
  viewer streams those as a live finger.)
- **WebDriverAgent.** Installs via AppSync and the developer image mounts, but launching
  the runner fails at `house_arrest` with `InstallationLookupFailed`. Developer services
  reject the AppSync-sideloaded container. Dead on this device.
- **Injecting arbitrary audio into a cellular call.** The call's audio is sealed in the
  baseband. During an active call, the software audio units render silence — measured at
  `rms = 0.00000` on both buses even under loud speech, across two separate diagnostics.
  Nothing in the iOS software stack carries that audio in either direction. The only thing
  that reaches the other party is `AVSpeechSynthesizer` through `mixToTelephonyUplink`,
  which is why `mimic_call` is text-to-speech only.
- **Capturing what the callee says on a cellular call.** Same wall, same reason. The
  downlink isn't in a readable software buffer. (A VoIP or FaceTime call would be a
  different story — that audio is in software and could be captured and transcribed — but
  that's not what `mimic_call` does.)
- **Playing a media file during a call.** The call owns the audio session, so
  `AVAudioPlayer` playback comes out silent. System sounds obey the physical mute switch.
- **Hooking `mediaserverd`'s audio render to grab call audio.** Putting a frida hook on
  that real-time thread trips the watchdog, which kills `mediaserverd` and drops
  SpringBoard into safe mode. Don't. (Recovery is `sbreload`.)

## Gotchas that bit during testing

- **SpringBoard safe mode from heavy injection.** Beyond the `mediaserverd` case above,
  running experimental native-pointer frida code *inside SpringBoard* can crash it into
  safe mode. Keep risky native code in a throwaway process; read what you need with plain
  Objective-C. Recovery: `sbreload` over SSH, then `touch mimic/ios/device.py` so the
  server rebuilds its controller and reattaches.
- **Don't `pkill ssh`.** The held SSH client is `frida-server`'s parent. Killing all SSH
  kills frida.
- **CARenderServer capture needs a per-frame autorelease pool.** The live JPEG path creates
  an autoreleased `UIImage` / JPEG `NSData` / base64 string each frame; without draining them
  per frame, at 30–60 fps memory climbs until jetsam reboots the device (this actually
  happened). Wrap each frame in an `NSAutoreleasePool` — the same thing the video recorder
  does — and it stays flat.
- **MCP server scope.** Registering the server at *local* scope ties it to one directory.
  If your client doesn't see it from elsewhere, register at user scope instead.
- **The wake-then-unlock path.** On this iOS version, the unlock call reports success but
  can leave the lock screen up. `mimic_wake_unlock` works around it by also pressing Home
  and re-checking the lock state until it clears — and it no-ops if the device is already
  unlocked, so it won't yank you out of an app.

## Not done

- WebDriverAgent integration (dead, see above).
- Two-way call audio (capture/transcribe the other party) — would require a VoIP/FaceTime
  path, not cellular.
- A bespoke no-frida introspection dylib for apps whose anti-tamper detects frida-gadget.
- Android. Removed early and not coming back.
