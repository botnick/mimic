# Architecture

Mimic is deliberately small. There is no framework, no plugin system, and no SDK. The
whole thing is a stdio server, one controller class, and two scripts that get injected
into the phone. This doc explains how those pieces fit and — more usefully — why the
design landed here after a couple of approaches that did not survive contact with the
hardware.

## The pieces

```
MCP client
   │  stdio, newline-delimited JSON-RPC 2.0
   ▼
mimic/server.py            tool list + dispatch (39 tools). No SDK; plain Python.
   │
   ▼
mimic/ios/device.py        IOSDevice — the controller. Owns the connections.
   ├── go-ios (tools/goios/ios)         launch · screenshot · app list   (usbmux/lockdown)
   └── frida-server :27042 (over USB)   everything that runs inside a process
         ├── SpringBoard  ← agent.js    read screen · tap · type · swipe · home/wake/unlock · video
         ├── InCallService ← agent.js   speak into the call uplink
         ├── SpringBoard  ← tel.js      dial · call state · hang up
         └── target app   ← agent.js    in-app accessibility (or via the gadget for hardened apps)
```

Two transports, split by what each is good at:

- **go-ios** speaks usbmux and lockdown over the USB cable. It launches apps, grabs a
  real screenshot, lists installed apps, and powers Mimic's device-info, battery, process,
  GPS-spoof (point or moving GPX route), pcap/syslog, app-install and file-container tools —
  plus the later batch: config profiles, process-kill, device-condition simulation (slow
  network / thermal), crash reports, low-level diag reads (disk / MobileGestalt), CPU-load
  sampling, language/locale, Safari/WebView JS over WebInspector, and host↔device port
  forwarding. All over USB, nothing injected — so they keep working on apps that reject a
  frida attach.
- **frida-server** is a signed binary running on the phone, reachable over a USB-forwarded
  port. Anything that has to reach inside a live process — walking the view hierarchy,
  firing a control action, setting a text field, recording the screen, speaking into a
  call — goes through a frida script attached to the right process.

`device.py` is the only place that knows about both. The server above it just maps tool
names to controller methods. The scripts below it (`agent.js`, `tel.js`) are the code
that actually runs on the device.

## How a "tap" really works

This is the core decision, so it is worth being concrete.

A tap is **not** a synthetic touch. `agent.js` walks the live `UIView` / accessibility
hierarchy, finds the element whose label matches, and fires its action directly:

- If the element (or an ancestor) is a `UIControl`, it calls
  `sendActionsForControlEvents:` — the same path a real `UIControl` tap takes.
- For table and collection cells, it invokes the delegate's
  `didSelectRowAtIndexPath:` / `didSelectItemAtIndexPath:`.
- Otherwise it falls back to `accessibilityActivate()`.

On the home screen there is no foreground app, so the same lookup runs against SpringBoard
instead — which is why tapping an app icon activates it and launches the app.

`mimic_look` returns those elements with on-screen coordinates so a human (or a model)
can reason about layout, but the coordinates are informational. The tap is dispatched by
identity, not by pixel, which is why it stays robust when things move around.

Typing is the same idea: find the `UITextField` / `UITextView`, set its value, and fire
`editingChanged` so the app's bindings notice.

## Why not raw touch injection

The first instinct is to synthesize `IOHIDEvent` touches and let the OS deliver them.
That was built — from SpringBoard injection all the way to a cross-compiled, entitled
touch daemon — and the result is consistent and frustrating: **pans and scrolls work,
discrete taps do not.** A synthetic drag moves a scroll view, so swiping pages the home
screen and scrolls some lists. But a synthetic tap never fires the discrete tap gesture
recognizers, so it never opens an app or presses a button. More than ten variations all
landed in the same place.

So tapping goes through accessibility instead, and that is why it works. The cost is the
edge cases below.

**Hardware buttons are a different path and do work.** Volume, mute, power/lock and Home
are not digitizer touches — they are Consumer-page `IOHIDEvent` keyboard usages dispatched
from SpringBoard (`agent.js` `consumerKey` / `hwkey`, exposed as `mimic_button`).
SpringBoard consumes those directly, so unlike synthetic taps they fire reliably.

## Why not WebDriverAgent

WDA is the usual answer for iOS automation. Here it installs fine (AppSync lets the
sideloaded runner past installd's signature check) and the developer image mounts, but
launching the runner fails at `house_arrest` with `InstallationLookupFailed` — the
developer services reject the AppSync-sideloaded container. WDA-launch is dead on this
device, so Mimic does not depend on it.

## Hardened apps and the gadget

Some apps detect a frida **attach** and refuse to run with it. For those, attaching is off
the table, so `mimic_look` inside them comes back empty with a note.

The workaround is to inject **frida-gadget at launch** through the tweak loader instead of
attaching. There is no ptrace attach for the app's checks to catch — the gadget is just
another dylib loaded at startup. It listens on a local port and the controller routes that
one bundle through it transparently.

This is persistence — a dylib that loads into one app on every launch — so it is a
**separate, opt-in step**, not something Mimic does on its own:

```bash
scripts/install_gadget_bypass.sh com.example.app   # scoped to exactly this bundle
scripts/install_gadget_bypass.sh --remove
```

The filter is scoped to the single bundle id you pass; it does not inject globally. Heavy
commercial anti-tamper can still spot the gadget by scanning loaded images, in which case
the next step would be a bespoke no-frida introspection dylib — that is not built.

## Calls and the telephony uplink

Making the *other* party hear synthesized speech is the part people assume is impossible,
so here is the mechanism.

`call_and_speak` dials through `tel.js` (a SpringBoard `openSensitiveURL` on a `tel://`
URL), polls `CTCallCenter` until the state reaches `connected` — that is the callee
actually answering — and then attaches `agent.js` to the **InCallService** process. There
it creates an `AVSpeechSynthesizer` with `mixToTelephonyUplink = YES` and
`usesApplicationAudioSession = NO`, and speaks. iOS mixes that synthesized audio into the
call's outgoing stream at the system level, so the person on the other end hears it.

Why it has to be this exact API: the call's audio — both directions — lives in the
baseband, not in any software buffer you can read or write. That is measured, not assumed
(see [TESTING.md](TESTING.md)). `mixToTelephonyUplink` is the one supported door into the
uplink, and it only carries `AVSpeechSynthesizer` output, which is why arbitrary audio
files cannot be injected into a cellular call.

## Screen capture and the live viewer

There are two ways Mimic gets pixels off the phone, and they trade setup against frame rate:

- **go-ios screenshot / MJPEG stream.** Fast, no injection — but it goes through the
  official screenshot service, which honours the per-window *secure* flag. DRM apps
  (Netflix, banking) come back black, and the stream caps around 9 fps.
- **CARenderServer over frida.** From SpringBoard, `agent.js` renders the composited
  display into an `IOSurface` (`CARenderServerRenderDisplay`), reads it back through a
  bitmap context, and JPEG-encodes it at ~40–60 fps (measured ~17 ms/frame). Like the
  screenshot service it reads the *capture* composite, so secure surfaces (FairPlay video,
  screenshot-protected apps) are still excluded — the win is frame rate, not DRM.
  `mimic_record` uses it for video; `device.frida_frame` exposes a single live frame.

The **live viewer** (`mimic/ios/viewer.py`) is a native desktop window built on this. It
composites a device frame with Pillow and pulls frames from either source (default
CARenderServer; a TURBO toggle switches to go-ios). It is backend-pluggable — Cocoa/AppKit
on macOS (the system Tk 8.5 freezes), Tkinter on Windows/Linux — and self-heals a stalled
capture while keeping the display awake. Clicks map to the nearest accessibility element
(tapping through the same path as `mimic_tap`), a drag becomes a live finger (digitizer
`down→move→up`) so scrolling follows the cursor, and the rail fires `mimic_button` presses.

## Configuration

`device.py` reads `mimic.config.json` (or `~/.mimic/config.json`) over a set of defaults,
so the same code adapts to different devices without edits:

```json
{
  "iproxy": "iproxy",
  "frida_port": 27042,
  "ssh_local_port": 44022,
  "ssh_device_port": 44,
  "ssh_pw": "alpine",
  "jb_prefix": "/var/jb",
  "gadget_port": 27052
}
```

`setup.sh` writes this after probing the device. The defaults match a palera1n rootless
iPhone over USB; a rootful jailbreak would set `jb_prefix` to `/` and `ssh_device_port` to
`22`.

## Keeping frida alive

`frida-server` is not run as a launch daemon by default (though `setup.sh` can install one).
Instead the controller starts it and holds it open over a backgrounded SSH session. The
practical consequence shows up in one place: **never broadly kill SSH.** That held SSH
client is `frida-server`'s parent process, so `pkill ssh` takes frida down with it. If you
need to clear SSH sessions, target them specifically.

## The hot-reload loop

The server caches the controller, but it stats `device.py` on every tool call. When the
file's mtime changes, it reloads the module and rebuilds the controller before serving the
call. Because `agent.js` and `tel.js` are read fresh on every attach, editing the device
side of things — Python or injected JavaScript — never requires a client reconnect. Only
the server's own tool list and dispatch (`server.py`) are fixed for the life of the
process. This is also the recovery path after a `sbreload`: touching `device.py` forces a
fresh controller that reattaches to the new SpringBoard.
