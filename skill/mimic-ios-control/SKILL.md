---
name: mimic-ios-control
description: >-
  Drive the connected iPhone through the Mimic MCP (tools named mimic_*): look at the
  screen, launch apps, tap, type, swipe, wake/unlock, press hardware buttons
  (volume / power-lock / mute), record video, mirror the screen live in a desktop window,
  place a call and speak TTS that the callee hears, operate anti-frida apps, and toggle the
  system-wide SSL certificate-pinning bypass (SSLKillSwitch3). Use this skill WHENEVER
  the user asks you to do anything on "the phone" / "มือถือ" / iPhone through Mimic —
  e.g. "เปิดแอพ X", "พิมพ์/ส่งข้อความใน LINE/IG/IDOL+", "โทรหา <เบอร์> แล้วบอกว่า...",
  "กดปุ่ม...", "อัดวิดีโอจอ", "เลื่อนฟีด", "เช็คว่าตอนนี้เปิดแอพอะไร", "ปิด/เปิด ssl pinning" — even when the
  user doesn't say the word "Mimic". It encodes the correct tool sequence and the
  proven dead-ends so you don't repeat experiments that crash the device into safe mode.
---

# Mimic iOS control

Mimic is an MCP that controls a real jailbroken iPhone "like a human" through a small
set of `mimic_*` tools. This skill is the operating manual: the right order to call the
tools in, how to recover when something breaks, and — just as important — the things
that are **already proven impossible on this device** so you don't waste turns (or trip
the watchdog into SpringBoard safe mode) rediscovering them.

Reference device: iPhone 8 · iOS 16.7.16 · palera1n rootless · **no passcode**.
Default TTS language is Thai (`th-TH`). Coordinates are screen points; `mimic_tap`/
`mimic_type` are label-based (they resolve a label from your last `mimic_look`), so you
rarely need raw coordinates.

## The core loop: look → act → verify

The single most important habit: **`mimic_look` before you `mimic_tap` or `mimic_type`.**
`mimic_look` returns a compact JSON list of actionable elements (`role`, `label`, `x`,
`y`) for ~100–200 tokens — far cheaper than a screenshot, and it gives you the exact
labels the tap/type tools expect. Tapping a label you guessed instead of one you just
read is the most common way to act on the wrong element or a stale screen.

A normal task looks like this:

```
mimic_wake_unlock()                         # once at the start of a session
mimic_launch(bundle="com.apple.MobileSMS")
mimic_look()                                # read labels + coords
mimic_tap(label="New Message")
mimic_look()                                # screen changed — look again
mimic_type(text="สวัสดี", field="To:")
mimic_tap(label="Send")
mimic_look()                                # verify it sent
```

Re-`look` after every action that changes the screen. The UI is live; your previous
element list is stale the moment you tap. When a tap has several matches for the same
label, pass `index` to pick the right one (`mimic_tap(label="Play", index=1)`).

Finding apps: `mimic_apps(query="line")` searches installed apps by name or bundle id,
returning bundle ids you can feed to `mimic_launch`. Use it instead of guessing bundle ids.

## When to reach for a screenshot

`mimic_look` reads the accessibility tree, which covers most native and standard UIKit
apps. Use `mimic_screenshot` (a full-screen PNG via go-ios, works for *any* app) when:

- `mimic_look` returns few/no elements but you can tell something is on screen (custom-drawn
  UI, games, web views, or an anti-frida app before the bypass is active).
- You need to confirm a *visual* result the a11y tree can't express (an image posted, a
  video playing, a layout looking right).
- The user explicitly wants to see the screen.

Screenshots cost far more tokens than `look`, so prefer `look` for navigation and reach
for a screenshot for confirmation or when `look` comes up empty. `mimic_record` captures
real *motion* to an mp4 (on-device) when a single frame won't tell the story — animations,
scrolling video, a live stream.

## Typing that actually registers

`mimic_type` sets the field value and fires `editingChanged`, which is what most apps
listen for. Target a specific field with `field="<label or placeholder>"`; without it
you hit the first text field. After typing, `mimic_look` to confirm the text landed in
the field you intended (some apps have hidden/placeholder fields that look identical).

Sending a chat message is usually `type` then `tap` the app's Send button — but some
chat apps send on the keyboard Return key, which is a separate process Mimic can't tap.
In those apps the send is triggered through the text view's delegate (a newline), which
Mimic's type path handles; if a message types in but won't send, `mimic_look` for an
on-screen send arrow/button and `mimic_tap` it rather than hunting for the keyboard key.

## Calling and making the callee hear TTS

`mimic_call(number, text)` is the headline capability and it is fully on-device:

1. It dials the number.
2. It **waits for the callee to actually answer** (`answer_timeout` seconds, default 40).
3. Only then does it speak `text` — synthesized speech is mixed into the call's
   **telephony uplink** (`mixToTelephonyUplink`), so the *callee* hears it on a normal
   cellular call. No speakerphone, no Mac, no acoustic relay.
4. By default it hangs up after speaking (`hang_after=true`); pass `false` to stay on the line.

```
mimic_call(number="0959979955", text="สวัสดีค่ะ อยากกินเนื้อทอด ทำให้หน่อยได้ไหมคะ")
```

Notes that matter:
- Only **AVSpeechSynthesizer system voices** reach the uplink — you cannot inject an
  arbitrary mp3 / recorded audio into a cellular call (proven, see dead-ends). Pick the
  message as text; the best-quality voice for the language is chosen automatically.
- `mimic_speak(text)` speaks on the device's **own speaker** (no call) — use it for local
  TTS, not for making a remote party hear something.
- `mimic_hangup()` ends the current call.

## Anti-frida apps (e.g. IDOL+ / com.xhxy.tala)

Some apps detect and reject a frida *attach*, so `mimic_look` inside them returns a note
that in-app elements are unavailable. The fix is the **gadget bypass**: it injects
frida-gadget at app launch (no ptrace attach to detect), scoped strictly to one bundle id.

This installs a persistent dylib, so it is a **separate step that requires the user's
explicit authorization** — never install it on your own initiative. When the user has
clearly authorized it ("ติดตั้งเลย" / "ทำเลย", not a vague "จัดการให้"):

```bash
scripts/install_gadget_bypass.sh com.xhxy.tala   # scoped to this ONE bundle
tools/goios/ios launch com.xhxy.tala             # relaunch so the gadget loads
# mimic_look / mimic_tap now work inside it (device.py routes through :27052 automatically)
scripts/install_gadget_bypass.sh --remove        # uninstall anytime
```

Run these from the repo root (`/Users/botnick/Desktop/mimic`). Heavy commercial RASP can
still detect frida-gadget by scanning loaded images; if the app keeps rejecting it after
the bypass, stop and tell the user — the next step (a bespoke no-frida dylib) isn't built.
Open such an app and do the whole flow in one quick sequence right after a fresh launch;
poking around slowly gives its runtime protection time to crash the app.

## Do NOT attempt these — proven dead-ends

These were each investigated exhaustively on this exact device. Re-attempting them wastes
turns and some of them **crash the phone into SpringBoard safe mode**. If a user asks for
one, explain the wall rather than experimenting:

- **Capturing or injecting cellular call audio in software** (recording what the callee
  says, injecting an mp3 into the call). Cellular call audio lives in the baseband chip —
  `mediaserverd` audio buffers read `rms=0.0000` during a call even under loud speech.
  Neither direction is reachable from iOS software. Only `mixToTelephonyUplink` (TTS →
  uplink) works, and only with system voices. Two-way "hear the callee" would require
  FaceTime/VoIP (capturable via the Speech framework), not a cellular call.
- **Hooking `mediaserverd AudioUnitRender`** (or any realtime audio thread): the watchdog
  kills mediaserverd → SpringBoard **safe mode**. Don't.
- **Playing an AVAudioPlayer/media file during a call** — the call owns the audio session,
  so it's silent. `AudioServicesPlaySystemSound` obeys the physical mute switch.
- **Tapping via raw IOHIDEvent / WebDriverAgent.** Synthetic touches drive scroll/pan but
  never fire discrete tap gesture recognizers (confirmed across 10+ recipes incl. an
  entitled daemon). WDA installs but dev-services reject the sideloaded runner at launch.
  Tap goes through accessibility — that's why `mimic_tap` is label-based. A custom,
  non-`UIControl` view (e.g. the Calculator keypad) may simply not be tappable; if
  `mimic_tap` doesn't fire on such an element, that's the known limit, not a bug to retry.
  (Hardware *buttons* are a different story: volume / power-lock / mute / home go through
  Consumer-HID keyboard usages — not the digitizer — and DO work. Use `mimic_button`;
  validated on-device, volume shows the HUD and `power` locks the screen.)

## Recovery when something breaks

- **A tool errors with a frida/connection failure**: frida-server is a held process, not a
  daemon. Just retry the call — `device.py`'s `ensure_frida()` restarts and re-holds it.
  Give it a moment; don't thrash.
- **SSH (dropbear) hangs or "Permission denied"**: usually a transient unauthenticated-client
  cap — retry. **Never broadly `pkill ssh`**: the keepalive ssh client is frida-server's
  parent process; killing it kills frida-server.
- **SpringBoard safe mode** (from heavy injection): recover with `sbreload` (or
  `launchctl reboot userspace`) over SSH — the jailbreak survives. Then frida-server and
  dropbear (manual processes) come back via `ensure_frida()` on the next tool call.
- **go-ios lost the developer image** (launch/screenshot fail): re-mount with
  `tools/goios/ios image auto --basedir=tools/goios/ddi`.
- If you edited `device.py`, the running MCP server must reconnect to pick it up
  (`agent.js`/`tel.js` are re-read on every attach, but `device.py` is loaded once).

## Quick tool reference

| Tool | Use |
|---|---|
| `mimic_wake_unlock` | Wake + unlock (no passcode). Run once at session start. |
| `mimic_look` | Read screen as compact actionable elements. **Call before every tap/type.** `all:true` adds non-actionable text. |
| `mimic_screenshot` | Full-screen PNG (any app). For visual confirmation or when `look` is empty. |
| `mimic_record` | Record live screen to mp4 (motion). |
| `mimic_apps` | Search installed apps → bundle ids. |
| `mimic_launch` | Launch app by bundle id. |
| `mimic_current_app` | Frontmost bundle id (`None` = home/locked). |
| `mimic_tap` | Tap an element by `label` (+`index` for duplicates). |
| `mimic_type` | Type into a field (`field` targets one; else first field). |
| `mimic_swipe` | Scroll/page `up`/`down`/`left`/`right` (`amount` repeats). |
| `mimic_home` | Press Home. |
| `mimic_button` | Press a hardware button: `home` / `volup` / `voldown` / `mute` / `power`. `power` is a SHORT press = lock the screen (it will NOT power off); volume shows the on-screen HUD. |
| `mimic_close` | Force-quit an app (`bundle`, default frontmost). |
| `mimic_call` | Call, wait for answer, speak TTS into the uplink so the **callee** hears it. |
| `mimic_speak` | Speak on the device's own speaker (no call). |
| `mimic_hangup` | End the current call. |
| `mimic_ssl` | Read/toggle the SSL Kill Switch 3 cert-pinning bypass. No args = status; `bypass:true/false` to set; `relaunch:<bundle>` to apply now. |
| `mimic_unpin` | Hook the FOREGROUND app's TLS trust checks (BoringSSL custom-verify + SecTrust) so a proxy can MITM its HTTPS — per-app, complements `mimic_ssl`. Launch the target app first. |
| `mimic_info` / `mimic_battery` / `mimic_ps` | Device info / battery / running processes (go-ios). |
| `mimic_location` | Spoof GPS: `lat`+`lon` to set, `reset:true` to restore. |
| `mimic_pcap` | Capture network packets to a `.pcap` for N `seconds` (optional `process`). |
| `mimic_syslog` | Capture device syslog to a file for N `seconds`. |
| `mimic_install` / `mimic_uninstall` | Sideload an `.ipa`/`.app` `path` / remove by `bundle`. |
| `mimic_files` | App-container files — `op`: tree / pull / push (`bundle`, `src`, `dst`, `path`). |
| `mimic_memlimit` | Lift a `process`'s jetsam memory limit (keep frida targets alive). |
| `mimic_assistivetouch` | AssistiveTouch `state`: enable / disable / toggle / get. |

## SSL pinning bypass (SSLKillSwitch3)

`mimic_ssl` controls NyaMisty's **SSLKillSwitch3** tweak (already installed on the
reference device) — the system-wide toggle for disabling TLS certificate validation,
which is what lets a proxy (Burp/mitmproxy) decrypt a hardened app's HTTPS for
inspection. It works by reading/writing the tweak's own prefs file
(`shouldDisableCertificateValidation` in
`…/com.nablac0d3.SSLKillSwitchSettings.plist`) directly via frida.

- `mimic_ssl()` → current state, e.g. `{"bypass": false, "found": true, "path": …}`.
- `mimic_ssl(bypass=true)` → disable cert validation (kill switch ON); `bypass=false` restores it.
- The tweak reads its setting **at each app's launch**, so a toggle applies to apps
  started *afterward*. To apply to an already-running app, pass `relaunch=<bundle>`
  (it kills + relaunches that one app), or just close and reopen the app yourself.
- `found:false` means the prefs file wasn't there yet — the tool still writes it, but
  double-check SSLKillSwitch3 is actually installed if a target app still pins.

`mimic_unpin` is the **per-app** route, and a useful complement. With the target app in the
foreground it frida-hooks the app's own trust checks — `SSL_set_custom_verify` /
`SSL_CTX_set_custom_verify` (the BoringSSL path `NSURLSession`/`CFNetwork` use) plus
`SecTrustEvaluate*` — so it catches pinning the system-wide tweak misses. It only reaches an
app frida can attach to (a frida-hardened app needs the gadget bypass), and is best called
right after launching the app, before its first request.

## Live screen viewer (desktop window)

To watch the phone in real time and drive it like scrcpy, there is a native desktop
window (no browser). It is cross-platform — macOS uses Cocoa/AppKit (the system Tk 8.5 is
broken there), Windows/Linux use Tk — picked automatically. The header shows live FPS +
frame latency:

```bash
python3 -m mimic.ios.viewer
#   macOS:        pip install pillow pyobjc-framework-Cocoa
#   Win / Linux:  pip install pillow
```

There is also a double-clickable `Mimic.app` (built by `scripts/build_app.sh`).

**Two capture sources, toggled by the rail's TURBO button:**
- **TURBO on (default) — CARenderServer over frida** (`device.frida_frame` / agent.js
  `frame`): renders the composited display at ~40-60 fps (vs go-ios' ~9) for a smooth
  mirror. It still respects the secure flag — FairPlay video / screenshot-protected apps
  black out the same as anywhere, so this is about frame rate, **not** capturing DRM. Needs
  the display awake (the viewer keeps it awake).
- **TURBO off — go-ios MJPEG**: ~9 fps, lighter on frida.

Controls (the SAME proven model as the MCP tools):
- **click** a UI element → nearest accessibility element → `tap_label`. Home-screen icons
  launch their app (tap_label falls back to SpringBoard there); in-app controls activate.
- **drag** → a live finger (digitizer `down→move→up`), so scrolling follows the cursor like
  holding the phone; **labelled rail** → Lock / Vol± / Mute / Home / Look / A11y, each with a
  press flash; **type** while focused, Enter sends.

Same wall as `mimic_tap`: custom-drawn views with no a11y element still can't be tapped.
The viewer **self-heals** (restarts a stalled source, keeps the display awake) and the
device frame is composited with Pillow, so the MCP server itself stays Frida-only.

## go-ios extras (USB tools beyond frida)

These wrap the bundled go-ios binary (lockdown / instruments over USB — no frida needed):

- **`mimic_info` / `mimic_battery` / `mimic_ps`** — device facts, battery, processes.
- **`mimic_location`** — spoof GPS (`lat`+`lon` to set, `reset:true` to restore; needs the
  developer image mounted).
- **`mimic_pcap`** — capture the device's network packets to a `.pcap` for N seconds
  (optionally one `process`) for traffic study — complements `mimic_ssl` + a proxy. (go-ios
  writes `dump-*.pcap`; the tool moves it to your `out` path.)
- **`mimic_syslog`** — dump the device syslog to a file for N seconds.
- **`mimic_install` / `mimic_uninstall`** — sideload an `.ipa`/`.app` / remove a bundle.
- **`mimic_files`** — app-container file ops (`op`: tree / pull / push) — e.g. pull an app's
  databases or caches out of its sandbox.
- **`mimic_memlimit`** — lift a process's jetsam memory limit (keeps frida-heavy targets
  from being killed).
- **`mimic_assistivetouch`** — toggle the on-screen AssistiveTouch home button.

Not wrapped: go-ios `httpproxy` requires a supervised `--p12file` cert, so for MITM use
`mimic_ssl` (pinning bypass) plus a manually-set proxy instead.
