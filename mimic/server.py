#!/usr/bin/env python3
"""Mimic MCP server — control an iOS device "like a human" over stdio.

Minimal, dependency-free MCP (JSON-RPC 2.0, newline-delimited) implementation so
it runs on the stock macOS python3.9 alongside the existing frida install.

Tools expose the PROVEN control model (see mimic/ios/device.py):
  look / screenshot / launch / apps / tap / type / swipe / scroll /
  home / wake_unlock / current_app.

Register:
  claude mcp add mimic -- python3 /Users/botnick/Desktop/mimic/mimic/server.py
"""
import base64
import importlib
import json
import os
import sys
import traceback

sys.path.insert(0, __file__.rsplit("/mimic/", 1)[0])
import mimic.ios.device as _devmod  # noqa: E402

_dev = None
_dev_mtime = -1.0

# Hot-reload while developing: server.py imports device.py once, but we re-check
# device.py's mtime on every tool call. If it changed on disk, reload the module
# and rebuild the IOSDevice so code edits take effect WITHOUT reconnecting the MCP.
# (agent.js / tel.js are already read fresh on each frida attach, so editing those
# needs no reload at all. Editing OTHER python modules — or server.py itself — still
# needs an MCP reconnect.) ensure_frida() is idempotent: it detects an already
# running frida-server and just reconnects, so a reload is cheap.
def _device_py_mtime() -> float:
    try:
        return os.path.getmtime(_devmod.__file__)
    except Exception:
        return -1.0


def dev():
    global _dev, _dev_mtime
    mt = _device_py_mtime()
    if _dev is None or mt != _dev_mtime:
        if _dev is not None:
            importlib.reload(_devmod)  # pick up edits to device.py
        _dev = _devmod.IOSDevice()
        _dev.ensure_frida()
        _dev_mtime = mt
    return _dev


def text(s):
    return {"type": "text", "text": s}


TOOLS = [
    {"name": "mimic_look",
     "description": "Read the current iOS screen as a compact list of actionable UI elements (app id + each element's role, label, and tap x,y). Token-cheap; use before tapping. Frida-hardened apps: in-app elements unavailable.",
     "inputSchema": {"type": "object", "properties": {
         "all": {"type": "boolean", "description": "include non-actionable text too (default false)"}}}},
    {"name": "mimic_screenshot",
     "description": "Capture a PNG screenshot of the whole iOS screen (via go-ios; works for any app). Returns the image.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "mimic_launch",
     "description": "Launch an app by bundle id (e.g. com.apple.mobilesafari).",
     "inputSchema": {"type": "object", "required": ["bundle"], "properties": {"bundle": {"type": "string"}}}},
    {"name": "mimic_apps",
     "description": "List/search installed apps. Pass a query to filter by name or bundle id.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}},
    {"name": "mimic_tap",
     "description": "Tap a UI element by its label (fires the element's accessibility / button action). Use a label from mimic_look.",
     "inputSchema": {"type": "object", "required": ["label"], "properties": {
         "label": {"type": "string"}, "index": {"type": "integer", "description": "which match (default 0)"}}}},
    {"name": "mimic_type",
     "description": "Type text into a text field. Optionally target a field by label/placeholder; default = first text field.",
     "inputSchema": {"type": "object", "required": ["text"], "properties": {
         "text": {"type": "string"}, "field": {"type": "string"}}}},
    {"name": "mimic_swipe",
     "description": "Swipe/scroll the screen (up/down/left/right) for scrolling and paging.",
     "inputSchema": {"type": "object", "required": ["direction"], "properties": {
         "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
         "amount": {"type": "integer", "description": "repeat count (default 1)"}}}},
    {"name": "mimic_home", "description": "Press Home (go to home screen / dismiss app).",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "mimic_button",
     "description": "Press a hardware button: home, volup, voldown, mute, or power. 'power' is a SHORT press = lock the screen (it will NOT power the device off). Volume changes are confirmed by the on-screen HUD.",
     "inputSchema": {"type": "object", "required": ["button"], "properties": {
         "button": {"type": "string", "enum": ["home", "volup", "voldown", "mute", "power"]}}}},
    {"name": "mimic_wake_unlock", "description": "Wake the display and unlock (no passcode).",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "mimic_current_app", "description": "Bundle id of the frontmost app.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "mimic_close",
     "description": "Force-quit (close) an app and remove it from the app switcher. Closes the frontmost app if no bundle is given.",
     "inputSchema": {"type": "object", "properties": {"bundle": {"type": "string", "description": "bundle id to close (default: frontmost)"}}}},
    {"name": "mimic_record",
     "description": "Record a VIDEO of the live screen (any app) to an mp4 file. Captures real motion. Returns the file path + stats.",
     "inputSchema": {"type": "object", "properties": {
         "seconds": {"type": "number", "description": "duration (default 5)"},
         "fps": {"type": "integer", "description": "frames/sec (default 10)"},
         "out": {"type": "string", "description": "output mp4 path (default /tmp/mimic_video.mp4)"}}}},
    {"name": "mimic_call",
     "description": "Place a phone call; once the callee ANSWERS, speak the given text so the CALLEE hears it — synthesized speech is mixed into the call's telephony uplink (mixToTelephonyUplink), fully on-device, works on a normal cellular call. Waits for answer before speaking.",
     "inputSchema": {"type": "object", "required": ["number", "text"], "properties": {
         "number": {"type": "string", "description": "phone number to call"},
         "text": {"type": "string", "description": "what to say after they answer"},
         "lang": {"type": "string", "description": "TTS language (default th-TH)"},
         "answer_timeout": {"type": "number", "description": "seconds to wait for answer (default 40)"},
         "hang_after": {"type": "boolean", "description": "hang up after speaking (default true)"}}}},
    {"name": "mimic_speak",
     "description": "Speak text aloud on the device speaker via TTS (no call). Thai voice by default.",
     "inputSchema": {"type": "object", "required": ["text"], "properties": {
         "text": {"type": "string"}, "lang": {"type": "string", "description": "default th-TH"}}}},
    {"name": "mimic_hangup", "description": "End the current phone call.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "mimic_ssl",
     "description": "SSL Kill Switch 3 control (NyaMisty) — read or toggle the system-wide SSL/TLS certificate-pinning bypass. No args = report current state. bypass=true DISABLES certificate validation (pinning bypassed — lets a proxy MITM/inspect an app's HTTPS); bypass=false restores normal validation. The tweak reads its setting at each app's launch, so a change applies on the NEXT launch of each app; pass relaunch=<bundle> to force-relaunch one app so it takes effect immediately.",
     "inputSchema": {"type": "object", "properties": {
         "bypass": {"type": "boolean", "description": "true = disable cert validation (kill switch ON), false = restore. Omit to just read state."},
         "relaunch": {"type": "string", "description": "bundle id to kill + relaunch so the change applies now"}}}},
    # --- go-ios powered extras (USB, independent of frida) ---
    {"name": "mimic_info",
     "description": "Device info via go-ios (model, iOS version, names, identifiers). Optional kind: 'display' or 'lockdown'.",
     "inputSchema": {"type": "object", "properties": {"kind": {"type": "string", "enum": ["display", "lockdown"]}}}},
    {"name": "mimic_battery",
     "description": "Battery level / charging state / temperature via go-ios.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "mimic_ps",
     "description": "List running processes (PIDs + names). apps=true lists only apps.",
     "inputSchema": {"type": "object", "properties": {"apps": {"type": "boolean"}}}},
    {"name": "mimic_location",
     "description": "Spoof the device GPS. Pass lat+lon to set, or reset=true to restore the real location. Needs the developer image mounted.",
     "inputSchema": {"type": "object", "properties": {
         "lat": {"type": "number"}, "lon": {"type": "number"}, "reset": {"type": "boolean"}}}},
    {"name": "mimic_pcap",
     "description": "Capture the device's network packets to a .pcap file for N seconds (optionally filtered to one process). For traffic study.",
     "inputSchema": {"type": "object", "properties": {
         "seconds": {"type": "number", "description": "duration (default 10)"},
         "process": {"type": "string", "description": "filter to one process name"},
         "out": {"type": "string", "description": "output .pcap path (default /tmp/mimic.pcap)"}}}},
    {"name": "mimic_syslog",
     "description": "Capture the device syslog to a text file for N seconds.",
     "inputSchema": {"type": "object", "properties": {
         "seconds": {"type": "number", "description": "duration (default 5)"},
         "out": {"type": "string", "description": "output path (default /tmp/mimic_syslog.txt)"}}}},
    {"name": "mimic_install",
     "description": "Install an app from an .ipa file or .app folder path.",
     "inputSchema": {"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}}},
    {"name": "mimic_uninstall",
     "description": "Uninstall an app by bundle id.",
     "inputSchema": {"type": "object", "required": ["bundle"], "properties": {"bundle": {"type": "string"}}}},
    {"name": "mimic_files",
     "description": "App-container file ops via go-ios fsync. op=tree lists files under path; op=pull copies device src -> local dst; op=push copies local src -> device dst. Requires the app's bundle id.",
     "inputSchema": {"type": "object", "required": ["op", "bundle"], "properties": {
         "op": {"type": "string", "enum": ["tree", "pull", "push"]},
         "bundle": {"type": "string"}, "src": {"type": "string"}, "dst": {"type": "string"},
         "path": {"type": "string", "description": "for op=tree (default /)"}}}},
    {"name": "mimic_memlimit",
     "description": "Lift the jetsam memory limit for a process (keeps frida-heavy targets from being killed).",
     "inputSchema": {"type": "object", "required": ["process"], "properties": {"process": {"type": "string"}}}},
    {"name": "mimic_assistivetouch",
     "description": "Manage AssistiveTouch (on-screen Home button). state: enable | disable | toggle | get.",
     "inputSchema": {"type": "object", "properties": {"state": {"type": "string", "enum": ["enable", "disable", "toggle", "get"]}}}},
]


def call_tool(name, args):
    d = dev()
    if name == "mimic_look":
        return [text(json.dumps(d.look(actionable_only=not args.get("all", False)), ensure_ascii=False))]
    if name == "mimic_screenshot":
        path = d.screenshot()
        with open(path, "rb") as f:
            return [{"type": "image", "data": base64.b64encode(f.read()).decode(), "mimeType": "image/png"}]
    if name == "mimic_launch":
        return [text("launched" if d.launch(args["bundle"]) else "launch failed")]
    if name == "mimic_apps":
        return [text(json.dumps(d.search_apps(args.get("query", ""))[:80], ensure_ascii=False))]
    if name == "mimic_tap":
        return [text(json.dumps(d.tap_label(args["label"], args.get("index", 0)), ensure_ascii=False))]
    if name == "mimic_type":
        return [text(json.dumps(d.type_text(args["text"], args.get("field", "")), ensure_ascii=False))]
    if name == "mimic_swipe":
        d.scroll(args["direction"], args.get("amount", 1)); return [text("ok")]
    if name == "mimic_home":
        d.home(); return [text("ok")]
    if name == "mimic_button":
        return [text(json.dumps(d.button(args["button"]), ensure_ascii=False))]
    if name == "mimic_wake_unlock":
        return [text(json.dumps(d.wake_unlock(), ensure_ascii=False))]
    if name == "mimic_current_app":
        return [text(str(d.current_app()))]
    if name == "mimic_close":
        return [text(json.dumps(d.close_app(args.get("bundle")), ensure_ascii=False))]
    if name == "mimic_record":
        r = d.record_video(seconds=args.get("seconds", 5), fps=args.get("fps", 10),
                           out=args.get("out", "/tmp/mimic_video.mp4"))
        return [text(json.dumps(r, ensure_ascii=False))]
    if name == "mimic_call":
        r = d.call_and_speak(args["number"], args["text"], lang=args.get("lang", "th-TH"),
                             answer_timeout=args.get("answer_timeout", 40),
                             hang_after=args.get("hang_after", True))
        return [text(json.dumps(r, ensure_ascii=False))]
    if name == "mimic_speak":
        return [text(json.dumps(d.speak(args["text"], args.get("lang", "th-TH")), ensure_ascii=False))]
    if name == "mimic_hangup":
        return [text(json.dumps(d.hangup(), ensure_ascii=False))]
    if name == "mimic_ssl":
        if "bypass" in args:
            r = d.ssl_set(bool(args["bypass"]), relaunch=args.get("relaunch"))
        else:
            r = d.ssl_status()
        return [text(json.dumps(r, ensure_ascii=False))]
    if name == "mimic_info":
        return [text(json.dumps(d.device_info(args.get("kind", "")), ensure_ascii=False))]
    if name == "mimic_battery":
        return [text(json.dumps(d.battery(), ensure_ascii=False))]
    if name == "mimic_ps":
        return [text(json.dumps(d.processes(args.get("apps", False)), ensure_ascii=False))]
    if name == "mimic_location":
        if args.get("reset"):
            return [text(json.dumps(d.reset_location(), ensure_ascii=False))]
        if "lat" in args and "lon" in args:
            return [text(json.dumps(d.set_location(args["lat"], args["lon"]), ensure_ascii=False))]
        return [text(json.dumps({"ok": 0, "error": "pass lat+lon to set, or reset=true"}))]
    if name == "mimic_pcap":
        return [text(json.dumps(d.pcap(args.get("seconds", 10), args.get("process"),
                                       args.get("out", "/tmp/mimic.pcap")), ensure_ascii=False))]
    if name == "mimic_syslog":
        return [text(json.dumps(d.syslog(args.get("seconds", 5),
                                         args.get("out", "/tmp/mimic_syslog.txt")), ensure_ascii=False))]
    if name == "mimic_install":
        return [text(json.dumps(d.install_app(args["path"]), ensure_ascii=False))]
    if name == "mimic_uninstall":
        return [text(json.dumps(d.uninstall_app(args["bundle"]), ensure_ascii=False))]
    if name == "mimic_files":
        return [text(json.dumps(d.files(args["op"], args["bundle"], args.get("src", ""),
                                        args.get("dst", ""), args.get("path", "/")), ensure_ascii=False))]
    if name == "mimic_memlimit":
        return [text(json.dumps(d.mem_unlimit(args["process"]), ensure_ascii=False))]
    if name == "mimic_assistivetouch":
        return [text(json.dumps(d.assistive_touch(args.get("state", "get")), ensure_ascii=False))]
    raise ValueError("unknown tool: " + name)


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        mid = req.get("id")
        method = req.get("method")
        try:
            if method == "initialize":
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mimic", "version": "0.1.0"}}})
            elif method == "notifications/initialized":
                pass
            elif method == "tools/list":
                send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
            elif method == "tools/call":
                p = req.get("params", {})
                content = call_tool(p.get("name"), p.get("arguments") or {})
                send({"jsonrpc": "2.0", "id": mid, "result": {"content": content, "isError": False}})
            elif method == "ping":
                send({"jsonrpc": "2.0", "id": mid, "result": {}})
            elif mid is not None:
                send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}})
        except Exception as e:
            if mid is not None:
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [text("ERROR: " + str(e) + "\n" + traceback.format_exc()[-400:])],
                    "isError": True}})


if __name__ == "__main__":
    main()
