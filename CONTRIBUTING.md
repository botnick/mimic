# Contributing / extending Mimic

The codebase is small on purpose, so adding to it is mostly a matter of knowing which of
the four files a change belongs in. This walks through the layout and then adds a tool end
to end as a worked example.

## Where things live

```
mimic/server.py      The MCP surface. Tool definitions (name, description, schema) and a
                     dispatch block that maps each tool to a controller method. This is
                     the only file the client talks to.

mimic/ios/device.py  IOSDevice — the controller. Owns the go-ios and frida connections,
                     attaches scripts to the right processes, and exposes one Python method
                     per capability. This is where most logic lives.

mimic/ios/agent.js   The frida runtime injected into SpringBoard / the foreground app /
                     InCallService. Reading the screen, tapping, typing, swiping, video,
                     and text-to-speech are all here, exported over frida RPC.

mimic/ios/tel.js     A second injected runtime for telephony: dial, call state, hang up.

mimic/ios/viewer.py  The live desktop viewer (AppKit on macOS, Tk on Windows/Linux).
                     Independent of the MCP server — it imports IOSDevice directly for
                     control and pulls frames from CARenderServer (frida) or go-ios.
```

The flow for any capability is the same: a method on `IOSDevice` calls an RPC export in
`agent.js` (or `tel.js`), and a tool in `server.py` calls that method.

## The development loop

The server hot-reloads the controller. It stats `device.py` on every tool call and, when
the file changes, reloads the module and rebuilds `IOSDevice` before serving the call.
`agent.js` and `tel.js` are read fresh on every attach. So:

- Editing **`device.py`**, **`agent.js`**, or **`tel.js`** → live on the next tool call.
  No reconnect.
- Editing **`server.py`** (adding or changing a tool, the JSON-RPC surface) → the client
  has to reconnect, because that is the long-lived process.

To iterate without a client at all, drive the server with raw JSON-RPC. A fresh process
picks up every change including `server.py`:

```bash
printf '%s\n%s\n%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"mimic_look","arguments":{}}}' \
  | python3 mimic/server.py
```

Or skip the protocol and call the controller directly:

```bash
python3 -c "import sys; sys.path.insert(0,'.'); \
from mimic.ios.device import IOSDevice; d=IOSDevice(); d.ensure_frida(); print(d.look())"
```

## frida RPC naming

One thing that trips people up: frida converts camelCase exports to snake_case on the
Python side. An export named `wakeUnlock` in `agent.js` is called as `.wake_unlock()` from
`device.py`. Keep the JS export camelCase and call it snake_case.

## Worked example: add a tool

Say you want a `mimic_brightness` tool that sets screen brightness.

**1. Add the work to `agent.js`** (it runs in SpringBoard, which can set brightness), and
export it:

```javascript
function setBrightness(level){
  try { ObjC.classes.UIScreen.mainScreen().setBrightness_(level); return 1; }
  catch(e){ return {err: '' + e}; }
}

// in rpc.exports:
setBrightness: function(level){ return setBrightness(level); },
```

**2. Add a method to `device.py`** that calls the export. `_springboard()` returns the
SpringBoard RPC object:

```python
def set_brightness(self, level: float) -> dict:
    return {"ok": bool(self._springboard().set_brightness(level))}
```

**3. Add the tool to `server.py`** — a definition in `TOOLS` and a branch in `call_tool`:

```python
{"name": "mimic_brightness",
 "description": "Set screen brightness (0.0–1.0).",
 "inputSchema": {"type": "object", "required": ["level"],
                 "properties": {"level": {"type": "number"}}}},
```

```python
if name == "mimic_brightness":
    return [text(json.dumps(d.set_brightness(args["level"]), ensure_ascii=False))]
```

**4. Try it.** Edits to `agent.js` and `device.py` are already live; the new `server.py`
tool shows up in a fresh process, so the pipe test above will exercise it. Reconnect your
client to pick up the new tool there.

That is the whole pattern. Most tools are a dozen lines across the three layers.

## Style and conventions

- Match the surrounding code. The Python targets stock `python3` with no third-party
  imports beyond `frida`; keep it that way so the server stays dependency-free.
- Frida calls into UIKit/AppKit objects belong on the main thread. Look at how
  `agent.js` schedules work on `ObjC.mainQueue` and waits for the result, and follow it.
- Be careful with native-pointer code inside SpringBoard. A bad pointer there can crash
  SpringBoard into safe mode. Prototype risky native code in a disposable process first.
- When you hit a wall, write it down in [docs/TESTING.md](docs/TESTING.md). A documented
  dead-end saves the next person the same day you just spent.

## Targeting a different device

The controller reads `mimic.config.json` (or `~/.mimic/config.json`) over a set of
defaults, so most differences are config, not code: jailbreak prefix, SSH port and
password, frida port. `setup.sh` writes that file after probing the device. If you bring
up Mimic on something other than a palera1n rootless iPhone, that file is the first place
to look.
