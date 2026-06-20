# Mimic

Drive a real iPhone from an LLM the way a person would: look at the screen, open
apps, tap, type, scroll, press its buttons, place a call, record a video. Mimic is a small
[Model Context Protocol](https://modelcontextprotocol.io) server that exposes those
actions as tools, so any MCP-capable assistant can operate the phone over USB — and a
[live viewer](#live-viewer) lets you watch and drive it by hand.

It is **accessibility-first**. Reading a screen returns a compact list of labeled
elements (~100–200 tokens) instead of a multi-thousand-token screenshot, which keeps
sessions cheap and fast. Screenshots and video are still there when you actually need
pixels.

This is iOS only. An Android port existed early on and was dropped on purpose — keeping
one platform working well beat keeping two half-working.

```
look → tap → type → look → tap   (the whole interaction loop)
```

---

## What actually works

Mimic is built around the control methods that hold up on a real device, not the ones
that look good on paper. Two of the obvious approaches — synthetic touch injection and
WebDriverAgent — were tried hard and abandoned (the [Architecture](docs/ARCHITECTURE.md)
doc explains why). What's left is a hybrid that is boring and reliable:

| Capability | Tool | Status |
|---|---|---|
| Read the screen as labeled, tappable elements | `mimic_look` | works |
| Full-screen PNG screenshot (any app) | `mimic_screenshot` | works |
| Record the live screen to an mp4 (real motion) | `mimic_record` | works |
| Launch / list / search apps | `mimic_launch`, `mimic_apps` | works |
| Tap an element by its label | `mimic_tap` | works (see limits) |
| Type into a text field | `mimic_type` | works |
| Scroll / page | `mimic_swipe` | works on most lists; some in-app scroll views ignore it |
| Home / wake / unlock (no passcode) | `mimic_home`, `mimic_wake_unlock` | works |
| Frontmost app / close an app | `mimic_current_app`, `mimic_close` | works |
| Call a number and speak to the person who answers | `mimic_call` | works |
| Speak on the device's own speaker | `mimic_speak` | works |
| Hang up | `mimic_hangup` | works |
| Toggle the system SSL pinning bypass | `mimic_ssl` | works |
| Per-app SSL unpinning (frida: BoringSSL + SecTrust) | `mimic_unpin` | works |
| Press hardware buttons (Vol±, Mute, Power/Lock, Home) | `mimic_button` | works |
| Device info / battery / running processes | `mimic_info`, `mimic_battery`, `mimic_ps` | works |
| Spoof GPS · capture pcap / syslog | `mimic_location`, `mimic_pcap`, `mimic_syslog` | works |
| Install / uninstall / pull app-container files | `mimic_install`, `mimic_uninstall`, `mimic_files` | works |
| Lift jetsam memory limit · toggle AssistiveTouch | `mimic_memlimit`, `mimic_assistivetouch` | works |

Twenty-nine tools, all validated on hardware (details in [docs/TESTING.md](docs/TESTING.md)).
There is also a **[live viewer](#live-viewer)** — a native desktop window that mirrors the
phone at up to ~60 fps and lets you click / type / swipe it like scrcpy.

A few of these are worth calling out because they are not obvious:

- **`mimic_call` speaks to the *other* person on a normal cellular call.** When the
  callee answers, synthesized speech is mixed into the call's outgoing audio using
  Apple's `AVSpeechSynthesizer.mixToTelephonyUplink`. No speakerphone, no second device
  held up to the mic, no audio played on your Mac. It is the real uplink. See
  [Calls and text-to-speech](#calls-and-text-to-speech).
- **`mimic_record` captures real video on the device.** macOS does not see this iPhone
  as a capture device, so frames are pulled off the render server, encoded on-device,
  and assembled into an mp4. You get actual motion, not a burst of stills.
- **`mimic_ssl` flips SSL Kill Switch 3.** If you run a debugging proxy, this is the
  switch that lets you read a pinned app's HTTPS. See
  [Inspecting app traffic](#inspecting-app-traffic).

---

## The reference device

Everything here was developed and tested on one setup. Other jailbroken iPhones on
iOS 16.x should work — the code reads a small config file so it adapts — but this is the
machine the "works" column above refers to:

> iPhone 8 · iOS 16.7.16 · **palera1n rootless** jailbreak · A11 (arm64e) · **no passcode**

Two things that will save you a bad afternoon:

- **Keep the passcode off.** On A11 chips, having a passcode set with this jailbreak can
  trip an SEP bootloop. The device under test has never had one.
- **Plug the iPhone straight into the Mac.** A USB hub drops the device mid-session. Use a
  cable into the machine itself.

The stack underneath: [go-ios](https://github.com/danielpaulus/go-ios) for
launch/screenshot/app-list over usbmux, and a signed `frida-server` (16.1.4, arm64e) for
everything that needs to reach inside a running process.

---

## Install

One command brings the whole thing up on a fresh jailbroken iPhone:

```bash
scripts/setup.sh
```

It detects the device, mounts the developer image, figures out the SSH setup and
jailbreak prefix, installs and signs `frida-server` (plus a launch daemon so it survives
a reboot), writes a per-device `mimic.config.json`, starts the USB tunnels, and registers
the MCP server. It is idempotent — run it again any time.

Useful flags:

```bash
scripts/setup.sh --ssh-pass mypass     # non-default root password
scripts/setup.sh --no-daemon           # start frida on demand instead of at boot
scripts/setup.sh --no-mcp              # skip MCP registration
scripts/setup.sh --frida-version X.Y.Z # download a specific frida-server
```

You need `iproxy` and `ldid` (both via Homebrew) and the `frida` Python package; the
script installs what's missing. `go-ios` and a working `frida-server` binary are bundled.

Sanity check without any client:

```bash
python3 mimic/ios/device.py look
```

### Register with an MCP client

`setup.sh` already does this for Claude Code. To register by hand:

```bash
claude mcp add mimic -- /usr/bin/python3 /absolute/path/to/mimic/mimic/server.py
```

For any other MCP client, point it at a **stdio** server with:

```
command: /usr/bin/python3
args:    ["/absolute/path/to/mimic/mimic/server.py"]
```

The server is pure Python with no third-party dependencies beyond `frida`. It runs on the
stock macOS `python3`.

---

## The 29 tools

| Tool | Arguments | What it does |
|---|---|---|
| `mimic_look` | `all?` | Read the screen as a list of `{role, label, x, y}`. Cheap; call it before every tap. `all:true` includes non-actionable text. |
| `mimic_screenshot` | — | Full-screen PNG of any app. |
| `mimic_record` | `seconds?`, `fps?`, `out?` | Record the live screen to an mp4. |
| `mimic_launch` | `bundle` | Open an app by bundle id. |
| `mimic_apps` | `query?` | List or search installed apps. |
| `mimic_tap` | `label`, `index?` | Tap an element by label; `index` disambiguates duplicates. |
| `mimic_type` | `text`, `field?` | Type into a field (first field, or one named by `field`). |
| `mimic_swipe` | `direction`, `amount?` | Scroll/page up/down/left/right. |
| `mimic_home` | — | Press Home. |
| `mimic_wake_unlock` | — | Wake the display and clear the lock screen (no passcode). |
| `mimic_current_app` | — | Bundle id of the frontmost app. |
| `mimic_close` | `bundle?` | Force-quit an app (frontmost if omitted). |
| `mimic_call` | `number`, `text`, `lang?`, `answer_timeout?`, `hang_after?` | Call, wait for an answer, then speak `text` into the uplink. |
| `mimic_speak` | `text`, `lang?` | Speak on the device's own speaker. |
| `mimic_hangup` | — | End the current call. |
| `mimic_ssl` | `bypass?`, `relaunch?` | Read or toggle the SSL Kill Switch 3 pinning bypass. |
| `mimic_unpin` | — | Hook the foreground app's TLS trust checks (BoringSSL custom-verify + SecTrust) so a proxy can read its HTTPS — per-app, complements `mimic_ssl`. |
| `mimic_button` | `button` | Press a hardware button: `home`/`volup`/`voldown`/`mute`/`power` (power = short press = lock). |
| `mimic_info` · `mimic_battery` · `mimic_ps` | `kind?` · — · `apps?` | Device info / battery / running processes (go-ios). |
| `mimic_location` | `lat`,`lon` or `reset` | Spoof the GPS, or reset to the real location. |
| `mimic_pcap` · `mimic_syslog` | `seconds?`, … | Capture device packets (.pcap) / syslog for N seconds. |
| `mimic_install` · `mimic_uninstall` | `path` · `bundle` | Sideload an .ipa/.app / remove an app. |
| `mimic_files` | `op`,`bundle`,… | App-container files: `tree` / `pull` / `push`. |
| `mimic_memlimit` · `mimic_assistivetouch` | `process` · `state` | Lift a process's jetsam limit / toggle AssistiveTouch. |

`mimic_tap` and `mimic_type` are coordinate-free: they resolve a label from your most
recent `mimic_look`, so they stay correct even as the layout shifts.

### A typical session

```
mimic_wake_unlock()
mimic_launch(bundle="com.apple.MobileSMS")
mimic_look()                          -> [{role:"btn", label:"New Message", x:350, y:66}, ...]
mimic_tap(label="New Message")
mimic_look()                          -> the compose screen
mimic_type(text="on my way", field="To:")
mimic_tap(label="Send")
mimic_look()                          -> confirm it sent
```

The rule that matters: **look again after anything that changes the screen.** The UI is
live and your previous element list is stale the moment you act.

---

## Calls and text-to-speech

`mimic_call(number, text)` dials, waits for the callee to actually pick up, and only then
speaks. The speech is mixed into the call's telephony uplink, so the person on the other
end hears it on an ordinary cellular call.

```
mimic_call(number="0812345678", text="Hi, running ten minutes late, see you soon")
```

Things worth knowing:

- Only **system speech voices** reach the uplink. You cannot push an arbitrary mp3 or a
  recorded clip into a cellular call — that path is sealed in the baseband
  (see [docs/TESTING.md](docs/TESTING.md) for the measurements that prove it). Pick the
  message as text and the best installed voice for the language is chosen automatically.
- `mimic_speak(text)` is the local version — it plays on the phone's own speaker and does
  not involve a call.
- The default language is Thai (`th-TH`); pass `lang` for anything else.

The underlying `call_and_speak` also accepts a `pitch` multiplier (0.5–2.0, where below 1
deepens the voice) if you want to shift the tone — handy when a language only ships a
single voice.

---

## Inspecting app traffic

If you run a debugging proxy (mitmproxy, Burp, …), `mimic_ssl` controls
[SSL Kill Switch 3](https://github.com/NyaMisty/ssl-kill-switch3), the tweak that disables
TLS certificate validation system-wide. That is what lets the proxy read a hardened app's
HTTPS, including apps that pin their certificates.

```
mimic_ssl()                                       # current state
mimic_ssl(bypass=true)                            # pinning off — proxy can decrypt
mimic_ssl(bypass=true, relaunch="com.some.app")   # also relaunch one app to apply now
mimic_ssl(bypass=false)                            # restore normal validation
```

The tweak reads its setting when an app launches, so a toggle takes effect on the *next*
launch. Pass `relaunch` to bounce one app immediately. With the bypass on, your proxy's
certificate does not even need to be trusted on the device — validation is simply skipped.

This requires SSL Kill Switch 3 to be installed on the device; Mimic only flips its
switch.

`mimic_unpin` is the **per-app** complement. With the target app in the foreground it injects
frida hooks into the app's own TLS trust checks — `SSL_set_custom_verify` /
`SSL_CTX_set_custom_verify` (the BoringSSL path `NSURLSession`/`CFNetwork` use) plus
`SecTrustEvaluate*` — so even pinning the system tweak doesn't catch is defeated. Launch the
app, call `mimic_unpin` (best before its first request), then point your proxy at the device.
A frida-hardened app needs the gadget bypass first.

---

## Live viewer

Sometimes you want to *watch* the phone, not just read it. `mimic/ios/viewer.py` is a
native desktop window — no browser — that mirrors the screen live and lets you drive it
with the mouse and keyboard, like scrcpy for a jailbroken iPhone.

```bash
python3 -m mimic.ios.viewer
#   macOS:        pip install pillow pyobjc-framework-Cocoa
#   Win / Linux:  pip install pillow
```

It is **cross-platform** (macOS → Cocoa/AppKit, since the system Tk 8.5 is broken;
Windows/Linux → Tk) and ships a double-clickable `Mimic.app` (`scripts/build_app.sh`).

Two capture sources, toggled by the **TURBO** button in the rail:

- **TURBO on (default) — CARenderServer over frida.** Renders the composited display at
  ~40–60 fps (vs ~9 for go-ios) for a smooth mirror. It still honours the secure flag, so
  FairPlay video and screenshot-protected apps black out the same as anywhere — this is
  about frame rate, not capturing DRM.
- **TURBO off — go-ios MJPEG.** ~9 fps, lighter on frida.

Click a UI element to tap it (home-screen icons launch their app), drag to scroll like a
real finger (a live `down → move → up` that follows the cursor), type to send text, and use
the labelled rail for Lock / Vol± / Mute / Home. It keeps the display awake and self-heals a
stalled capture; the header shows live FPS and frame latency.

---

## The skill

[`skill/mimic-ios-control`](skill/mimic-ios-control/SKILL.md) is a skill that teaches an
assistant how to drive Mimic well — the look-act-verify loop, when to reach for a
screenshot instead of `look`, how calls and the SSL toggle behave, and the dead-ends that
are not worth re-discovering. If your client supports skills, drop it in:

```bash
cp -r skill/mimic-ios-control ~/.claude/skills/
```

It is plain Markdown with a small front-matter block, so it is easy to read and adapt.

---

## Developing against it

The server hot-reloads the controller while it runs. Editing `mimic/ios/device.py` takes
effect on the next tool call with no reconnect, and the injected scripts
(`agent.js`, `tel.js`) are re-read on every attach. Only changes to `server.py` itself —
adding a tool, changing the JSON-RPC surface — need the client to reconnect.

To test a change fast without any client, pipe JSON-RPC straight into the server:

```bash
printf '%s\n%s\n%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"mimic_current_app","arguments":{}}}' \
  | python3 mimic/server.py
```

A fresh process loads all your new code, which is the quickest way to check an edit.
[CONTRIBUTING.md](CONTRIBUTING.md) walks through adding a tool end to end.

---

## Recovery

Nothing here needs DFU. The common cases:

- **A tool fails with a frida/connection error.** `frida-server` is a held process, not a
  daemon. Just retry — the controller restarts it on the next call. Don't thrash.
- **SSH hangs or says "Permission denied."** Usually a transient dropbear cap. Retry.
  Never run a broad `pkill ssh`: the keep-alive SSH client is `frida-server`'s parent, and
  killing it kills frida.
- **SpringBoard drops into safe mode** (heavy injection can trip the watchdog). Run
  `sbreload` over SSH. The jailbreak survives. If the running server then complains about a
  destroyed script, `touch mimic/ios/device.py` to force the hot-reload and reattach.

---

## Layout

```
mimic/
  server.py            MCP server — the entry point (stdio JSON-RPC, no SDK)
  ios/
    device.py          IOSDevice — the controller (go-ios + frida hybrid)
    agent.js           injected runtime: read screen, tap, type, swipe, video, TTS,
                       hardware buttons, CARenderServer capture, ...
    tel.js             telephony: dial, call state, hang up
    viewer.py          live desktop viewer (AppKit on macOS, Tk on Windows/Linux)
    icon.png           viewer app icon
scripts/
  setup.sh             one-command bring-up
  build_app.sh         build a double-clickable Mimic.app for the viewer
  install_gadget_bypass.sh   opt-in anti-frida bypass for one app
assets/                viewer icon (png + icns)
skill/
  mimic-ios-control/   the companion skill
tools/                 bundled go-ios + signed frida-server + gadget
docs/
  ARCHITECTURE.md      how it's put together and why
  TESTING.md           what was tested, on what, and where the edges are
```

---

## Responsible use

Mimic drives a phone, places calls, and can strip TLS pinning so a proxy reads an app's
traffic. Use it on devices you own and accounts you control, or where you have explicit
permission to test. Intercepting traffic, automating calls, or operating someone else's
device without consent is on you, not the tool.

## License

MIT. See [LICENSE](LICENSE).
