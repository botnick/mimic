#!/usr/bin/env python3
"""Mimic iOS live viewer — a premium, scrcpy-style cross-platform window for the iPhone.

A native desktop window (no browser) that mirrors the jailbroken device LIVE over USB and
drives it, reusing the proven control model in mimic/ios/device.py:

    * live screen   : go-ios MJPEG stream (full-res JPEG over the USB DVT channel)
    * click          : nearest accessibility element from look() -> tap_label()
    * drag           : swipe() (pixel coords)
    * labelled rail  : Lock / Vol+ / Vol- / Mute / Home / Look / A11y  (+ press flash)
    * side buttons   : clickable nubs on the device frame
    * type           : type while focused; Enter sends -> type_text()

ARCHITECTURE — the whole UI (device body, rounded screen, header, button rail) is
composited with Pillow into one image; mouse clicks are hit-tested against that layout.
The GUI shell is pluggable so it runs everywhere:
    * macOS  -> AppKit / Cocoa  (the system Python ships a broken Tk 8.5 that freezes,
                                  so we drive Cocoa directly)
    * Windows / Linux -> Tkinter (Tk 8.6 there works fine)
The platform-agnostic `Engine` holds all the logic; each backend only pumps frames and
forwards mouse/keyboard. Layout is fully dynamic — it reflows to any window size.

Run:  python3 -m mimic.ios.viewer
  macOS:        pip install pillow pyobjc-framework-Cocoa
  Win / Linux:  pip install pillow            (Tk ships with Python)
"""
from __future__ import annotations

import io
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from urllib.request import urlopen

from PIL import Image, ImageDraw, ImageFilter, ImageFont

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from mimic.ios.device import IOSDevice, GOIOS  # noqa: E402

SCREEN_PTS = (375, 667)
SCREEN_PX = (750, 1334)
STREAM_PORT = 3333
STREAM_URL = "http://127.0.0.1:%d/" % STREAM_PORT
HEADER_H = 46

P = {
    "bg": (10, 11, 15), "panel": (18, 20, 32), "body": (14, 15, 21),
    "body_edge": (58, 63, 75), "bevel": (32, 36, 46), "screen_bg": (0, 0, 0),
    "nub": (23, 26, 33), "nub_edge": (51, 56, 69), "btn": (28, 31, 43),
    "btn_on": (10, 132, 255), "accent": (10, 132, 255), "green": (48, 209, 88),
    "amber": (255, 159, 10), "red": (255, 69, 58), "text": (242, 243, 247),
    "dim": (135, 141, 158), "icon": (214, 218, 230),
}


def _font(size, bold=False):
    cands = (["/System/Library/Fonts/SFNSDisplay.ttf", "/System/Library/Fonts/SFNS.ttf",
              "/Library/Fonts/Arial Bold.ttf", "C:/Windows/Fonts/arialbd.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
             if bold else
             ["/System/Library/Fonts/SFNS.ttf", "/Library/Fonts/Arial.ttf",
              "C:/Windows/Fonts/arial.ttf", "/System/Library/Fonts/Helvetica.ttc",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"])
    for p in cands:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


F_TITLE, F_BTN, F_MONO = _font(17, True), _font(10, True), _font(11)


def rr(d, box, radius, **kw):
    d.rounded_rectangle(box, radius=radius, **kw)


# ============================================================== device-frame render
def build_chrome(W, H):
    sw0, sh0 = SCREEN_PX
    bez = 0.035 * sw0
    pw0, ph0 = sw0 + 2 * bez, sh0 + 2 * bez
    pad = 16
    scale = min((W - 2 * pad) / pw0, (H - 2 * pad) / (ph0 * 1.04))
    PW, PH = max(1, int(pw0 * scale)), max(1, int(ph0 * scale))
    SW, SH = max(1, int(sw0 * scale)), max(1, int(sh0 * scale))
    b = int(bez * scale)
    body_r = int(PW * 0.11)
    scr_r = max(6, body_r - b)
    px, py = (W - PW) // 2, (H - PH) // 2
    sx, sy = px + b, py + b

    img = Image.new("RGBA", (W, H), P["bg"] + (255,))
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle([px, py + int(PH * 0.03), px + PW, py + PH + int(PH * 0.03)],
                                         radius=body_r, fill=(0, 0, 0, 150))
    img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(max(6, int(PW * 0.05)))))
    d = ImageDraw.Draw(img)

    nub_w = max(3, int(b * 0.46))
    nubs = {}

    def nub(name, side, cyf, lf):
        ln = int(SH * lf)
        cyy = py + b + int(SH * cyf)
        if side == "L":
            x1, x2 = px - nub_w, px + int(b * 0.4)
        else:
            x1, x2 = px + PW - int(b * 0.4), px + PW + nub_w
        rr(d, [x1, cyy, x2, cyy + ln], nub_w, fill=P["nub"] + (255,), outline=P["nub_edge"] + (255,), width=1)
        nubs[name] = (min(x1, x2) - 4, cyy - 3, max(x1, x2) + 4, cyy + ln + 3)

    nub("power", "R", 0.17, 0.13)
    nub("mute", "L", 0.10, 0.05)
    nub("volup", "L", 0.205, 0.11)
    nub("voldown", "L", 0.345, 0.11)

    rr(d, [px, py, px + PW, py + PH], body_r, fill=P["body"] + (255,), outline=P["body_edge"] + (255,), width=2)
    rr(d, [px + 2, py + 2, px + PW - 2, py + PH - 2], body_r - 2, outline=P["bevel"] + (150,), width=1)
    rr(d, [sx, sy, sx + SW, sy + SH], scr_r, fill=P["screen_bg"] + (255,))
    slit_w, slit_h = int(SW * 0.16), max(3, int(b * 0.22))
    slx, sly = px + (PW - slit_w) // 2, py + (b - slit_h) // 2
    rr(d, [slx, sly, slx + slit_w, sly + slit_h], slit_h // 2, fill=(40, 44, 54, 255))
    cam = max(2, int(b * 0.13))
    d.ellipse([slx + slit_w + int(b * 0.45), sly, slx + slit_w + int(b * 0.45) + cam, sly + cam],
              fill=(28, 32, 40, 255))

    mask = Image.new("L", (SW, SH), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, SW, SH], radius=scr_r, fill=255)
    return img, {"screen": (sx, sy, SW, SH), "nubs": nubs}, mask


# ---------------------------------------------------------------- PIL button icons
def _ic_power(d, x, y, c):
    d.arc([x - 8, y - 8, x + 8, y + 8], 290, 250, fill=c, width=2)
    d.line([x, y - 10, x, y - 1], fill=c, width=2)


def _ic_home(d, x, y, c):
    d.ellipse([x - 8, y - 8, x + 8, y + 8], outline=c, width=2)
    rr(d, [x - 3, y - 3, x + 3, y + 3], 1, outline=c, width=2)


def _ic_look(d, x, y, c):
    d.arc([x - 8, y - 8, x + 8, y + 8], -40, 210, fill=c, width=2)
    d.polygon([x + 8, y - 9, x + 11, y - 2, x + 3, y - 3], fill=c)


def _ic_boxes(d, x, y, c):
    for dx, dy in ((-7, -7), (1, -7), (-7, 1), (1, 1)):
        rr(d, [x + dx, y + dy, x + dx + 6, y + dy + 6], 1, outline=c, width=2)


def _ic_volplus(d, x, y, c):
    d.line([x - 8, y, x + 8, y], fill=c, width=2)
    d.line([x, y - 8, x, y + 8], fill=c, width=2)


def _ic_volminus(d, x, y, c):
    d.line([x - 8, y, x + 8, y], fill=c, width=2)


def _ic_mute(d, x, y, c):
    d.polygon([(x - 9, y - 4), (x - 4, y - 4), (x + 1, y - 9), (x + 1, y + 9),
               (x - 4, y + 4), (x - 9, y + 4)], fill=c)
    d.line([(x + 4, y - 7), (x + 11, y + 7)], fill=P["red"], width=2)


def _ic_drm(d, x, y, c):  # lightning bolt = turbo (high-fps CARenderServer) capture
    d.polygon([(x + 2, y - 9), (x - 7, y + 2), (x - 1, y + 2), (x - 3, y + 9),
               (x + 7, y - 3), (x, y - 3)], fill=c)


# ----------------------------------------------------------------- full UI compose
_CHROME = {}
RAIL_W = 94
_COLS = {"red": P["red"], "icon": P["icon"], "accent": P["accent"], "green": P["green"],
         "amber": P["amber"]}
_ICONS = {"power": _ic_power, "volup": _ic_volplus, "voldown": _ic_volminus,
          "mute": _ic_mute, "home": _ic_home, "look": _ic_look, "boxes": _ic_boxes,
          "drm": _ic_drm}
RAIL_BTNS = [("power", "LOCK", "red"), ("volup", "VOL +", "icon"), ("voldown", "VOL −", "icon"),
             ("mute", "MUTE", "icon"), ("home", "HOME", "accent"), ("look", "LOOK", "accent"),
             ("boxes", "A11Y", "green"), ("drm", "TURBO", "amber")]


def compose(W, H, frame, *, status, conn, boxes_on, nub_hi, typebuf, fps=0, ms=0, flash=None, drm_on=False):
    img = Image.new("RGBA", (W, H), P["bg"] + (255,))
    d = ImageDraw.Draw(img)
    cy = HEADER_H // 2
    # header: dot · Mimic · FPS · ms · status
    d.rectangle([0, 0, W, HEADER_H], fill=P["panel"] + (255,))
    cc = {"live": P["green"], "connecting": P["amber"], "error": P["red"]}[conn]
    d.ellipse([16, cy - 5, 26, cy + 5], fill=cc + (255,))
    d.text((36, cy - 11), "Mimic", font=F_TITLE, fill=P["text"] + (255,))
    d.text((114, cy - 8), "%d FPS" % fps, font=F_BTN, fill=P["green"] + (255,))
    d.text((166, cy - 8), "%d ms" % ms, font=F_BTN, fill=P["dim"] + (255,))
    msg = typebuf and ("type: " + typebuf) or status
    d.text((226, cy - 7), msg[:24], font=F_MONO, fill=(P["accent"] if typebuf else P["dim"]) + (255,))

    # phone stage (left of the rail)
    sw, sh = W - RAIL_W, H - HEADER_H
    cached = _CHROME.get((sw, sh))
    if cached is None:
        cached = _CHROME[(sw, sh)] = build_chrome(sw, sh)
    base, geom, mask = cached
    chrome = base.copy()
    sx, sy, SW, SH = geom["screen"]
    if frame is not None:
        try:
            chrome.paste(frame.resize((SW, SH), Image.BILINEAR), (sx, sy), mask)
        except Exception:
            pass
    if nub_hi and nub_hi in geom["nubs"]:
        x1, y1, x2, y2 = geom["nubs"][nub_hi]
        ImageDraw.Draw(chrome).rounded_rectangle([x1 + 4, y1 + 3, x2 - 4, y2 - 3], radius=4,
                                                 outline=P["accent"] + (255,), width=2)
    img.alpha_composite(chrome, (0, HEADER_H))

    # right rail: clearly-labelled buttons with press flash
    d.rectangle([W - RAIL_W, HEADER_H, W, H], fill=P["panel"] + (255,))
    n = len(RAIL_BTNS)
    bh = min(66, max(46, (H - HEADER_H - 16) // n - 8))
    gap = 8
    total = n * bh + (n - 1) * gap
    y0 = HEADER_H + max(8, ((H - HEADER_H) - total) // 2)
    bx0, bx1 = W - RAIL_W + 8, W - 8
    btns = {}
    for i, (name, label, ckey) in enumerate(RAIL_BTNS):
        by0 = y0 + i * (bh + gap)
        by1 = by0 + bh
        lit = (name == "boxes" and boxes_on) or (name == "drm" and drm_on) or (flash == name)
        rr(d, [bx0, by0, bx1, by1], 13, fill=(_COLS[ckey] if lit else P["btn"]) + (255,))
        _ICONS[name](d, (bx0 + bx1) // 2, by0 + bh // 2 - 7, P["text"] if lit else _COLS[ckey])
        tw = d.textlength(label, font=F_BTN)
        d.text(((bx0 + bx1) / 2 - tw / 2, by1 - 15), label, font=F_BTN,
               fill=(P["text"] if lit else P["dim"]) + (255,))
        btns[name] = (bx0, by0, bx1, by1)

    off = HEADER_H
    hit = {"screen": (sx, sy + off, SW, SH),
           "nubs": {k: (v[0], v[1] + off, v[2], v[3] + off) for k, v in geom["nubs"].items()},
           "buttons": btns}
    return img, hit


# --------------------------------------------------------------------------- MJPEG
class MJPEGStream(threading.Thread):
    def __init__(self, url):
        super().__init__(daemon=True)
        self.url, self.latest, self.alive = url, None, True
        self.connected, self.error = False, None
        self.seq = 0

    def run(self):
        while self.alive:
            try:
                resp = urlopen(self.url, timeout=10)
                self.connected, self.error = True, None
                buf = b""
                while self.alive:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    buf += chunk
                    end = buf.rfind(b"\xff\xd9")
                    if end != -1:
                        start = buf.rfind(b"\xff\xd8", 0, end)
                        if start != -1:
                            self.latest = buf[start:end + 2]
                            self.seq += 1
                            buf = buf[end + 2:]
                    if len(buf) > 4_000_000:
                        buf = buf[-1_000_000:]
            except Exception as e:  # noqa: BLE001
                self.connected, self.error = False, str(e)
                time.sleep(1.0)

    def stop(self):
        self.alive = False


def _port_open(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def ensure_stream_server(port=STREAM_PORT):
    # Always start a FRESH stream: a stale go-ios stream can hold the port open but stop
    # delivering frames (frozen black screen), so kill any existing one first.
    try:
        subprocess.run(["pkill", "-f", "screenshot --stream --port=%d" % port],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    time.sleep(0.6)
    proc = subprocess.Popen([GOIOS, "screenshot", "--stream", "--port=%d" % port],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(25):
        if _port_open(port):
            break
        time.sleep(0.3)
    return proc


class FridaSource(threading.Thread):
    """High-fps, DRM-bypassing capture via CARenderServer over frida (from SpringBoard).
    Grabs the real composited display BELOW the secure layer, so it mirrors Netflix /
    banking apps that go-ios screenshots show black — and it runs ~40-60 fps. Exposes the
    same .latest / .connected interface as MJPEGStream so the engine can swap sources."""

    def __init__(self, dev, quality=0.45):
        super().__init__(daemon=True)
        self.dev, self.q = dev, quality
        self.latest, self.alive, self.connected, self.error = None, True, False, None
        self.seq = 0

    def run(self):
        while self.alive:
            try:
                b = self.dev.frida_frame(self.q)
                if b:
                    self.latest = b
                    self.seq += 1
                    self.connected = True
                    time.sleep(0.02)                # ~25 fps: safe + leaves frida for live touch
                else:
                    self.connected = False
                    time.sleep(0.1)
            except Exception as e:  # noqa: BLE001
                self.connected, self.error = False, str(e)
                time.sleep(0.3)

    def stop(self):
        self.alive = False


# ===================================================== platform-agnostic engine ====
class Engine:
    """All the logic: stream, frida control, compositing, hit-testing. Produces a PIL
    image (self.next_pil) for whatever GUI backend is driving it; backends forward
    mouse/keyboard in TOP-LEFT window coordinates."""

    def __init__(self):
        self.dev = IOSDevice()
        self.elements, self.boxes = [], False
        self.status, self.conn = "starting…", "connecting"
        self.typebuf = ""
        self.fps, self.lat_ms = 0, 0
        self.wh = (0, 0)
        self.next_pil = None
        self._jobs = queue.Queue()
        self._press = None
        self._nub_hi = None
        self._hit = None
        self._flash = None
        self._owned = None
        self._last_frame = time.time()
        self._fid = None
        self._last_wake = 0.0
        self.drm = True                              # default source: CARenderServer (fast + DRM-bypass)
        self.stream = MJPEGStream(STREAM_URL)        # go-ios fallback, lazy-started
        self.frida_src = FridaSource(self.dev)       # CARenderServer source, started in _boot
        self.source = self.frida_src
        self._touchq = queue.Queue()
        self._dragging = False
        for fn in (self._boot, self._worker, self._render_loop, self._watchdog, self._touch_loop):
            threading.Thread(target=fn, daemon=True).start()

    # ---- worker / boot ----
    def _set(self, s):
        self.status = s

    def _do(self, name, fn):
        self._jobs.put((name, fn))

    def _btn(self, name):
        self._do(name, lambda: self.dev.button(name))

    def _worker(self):
        while True:
            name, fn = self._jobs.get()
            try:
                r = fn()
                self._set("%s · %s" % (name, r if isinstance(r, (str, dict)) else "ok"))
            except Exception as e:  # noqa: BLE001
                self._set("✗ %s: %s" % (name, e))

    def _boot(self):
        self._set("connecting frida…")
        try:
            self.dev.ensure_frida()
            try:
                self.dev.wake_unlock()
                self.dev.keep_awake()
            except Exception:
                pass
            self.frida_src.start()                   # CARenderServer (default source)
            self._look()
            self._set("ready · TURBO")
        except Exception as e:  # noqa: BLE001
            self._set("frida error: %s" % e)

    def _look(self):
        r = self.dev.look()
        self.elements = r.get("elements", []) if isinstance(r, dict) else []
        app = r.get("app") if isinstance(r, dict) else "?"
        self._set("look: %s · %d el" % (app, len(self.elements)))
        return app

    def toggle_drm(self):
        """Switch capture source: CARenderServer (frida, fast + DRM-bypass) <-> go-ios MJPEG."""
        self.drm = not self.drm
        if self.drm:
            if not self.frida_src.is_alive():
                self.frida_src = FridaSource(self.dev)
                self.frida_src.start()
            self.source = self.frida_src
            self._set("TURBO ON · CARenderServer (fast + DRM-bypass)")
        else:
            if not self.stream.is_alive():
                ensure_stream_server()
                self.stream = MJPEGStream(STREAM_URL)
                self.stream.start()
            self.source = self.stream
            self._set("TURBO OFF · go-ios MJPEG")
        self._last_frame = time.time()

    # ---- render thread: heavy PIL work, as fast as frames arrive ----
    def _render_loop(self):
        last_id, last_ui, last_draw = None, None, 0.0
        cnt, t_fps = 0, time.time()
        while True:
            W, H = self.wh
            if W < 60 or H < 60:
                time.sleep(0.02)
                continue
            self.conn = ("live" if self.source.connected
                         else "error" if self.source.error else "connecting")
            cur = self.source.latest
            seq = self.source.seq
            now = time.time()
            if seq != self._fid:
                self._fid = seq
                self._last_frame = now
            flash = self._flash[0] if (self._flash and now < self._flash[1]) else None
            ui = (self.status, self._nub_hi, self.boxes, self.typebuf, self.conn, flash, self.drm, W, H)
            if seq == last_id and ui == last_ui and now - last_draw < 0.2:
                time.sleep(0.003)
                continue
            last_id, last_ui, last_draw = seq, ui, now
            t0 = time.time()
            frame = None
            if cur is not None:
                try:
                    frame = Image.open(io.BytesIO(cur)).convert("RGB")
                except Exception:
                    frame = None
            try:
                img, hit = compose(W, H, frame, status=self.status, conn=self.conn,
                                   boxes_on=self.boxes, nub_hi=self._nub_hi,
                                   typebuf=self.typebuf, fps=self.fps, ms=self.lat_ms,
                                   flash=flash, drm_on=self.drm)
                self._hit = hit
                self.next_pil = img
            except Exception:
                continue
            self.lat_ms = int(self.lat_ms * 0.7 + (time.time() - t0) * 1000 * 0.3)
            cnt += 1
            if now - t_fps >= 0.5:
                self.fps = int(round(cnt / (now - t_fps)))
                cnt, t_fps = 0, now

    # ---- self-healing watchdog: restart a stalled stream, keep the display awake ----
    def _watchdog(self):
        while True:
            time.sleep(2.0)
            now = time.time()
            if now - self._last_wake > 18:          # keep display awake -> no black-from-sleep
                self._last_wake = now
                try:
                    self.dev.keep_awake()
                except Exception:
                    pass
            if self.source.connected and now - self._last_frame > 4.0:
                self._set("source stalled — self-healing…")
                try:
                    self.dev.wake()                  # likely the display slept -> black
                except Exception:
                    pass
                if not self.drm:                     # go-ios mode: respawn the stale stream
                    try:
                        self.stream.stop()
                        ensure_stream_server()
                        self.stream = MJPEGStream(STREAM_URL)
                        self.stream.start()
                        self.source = self.stream
                    except Exception as e:  # noqa: BLE001
                        self._set("heal failed: %s" % e)
                self._last_frame = time.time()

    # ---- input (TOP-LEFT window coords from the backend) ----
    def _hit_in(self, group, x, y):
        for n, (x1, y1, x2, y2) in (self._hit or {}).get(group, {}).items():
            if x1 <= x <= x2 and y1 <= y <= y2:
                return n
        return None

    def on_down(self, x, y):
        self._press = (x, y)
        self._dragging = False
        self._nub_hi = self._hit_in("nubs", x, y)

    def on_move(self, x, y):
        """Live finger drag — real scrolling that follows the cursor (digitizer touch)."""
        if self._press is None or self._nub_hi:
            return
        if not self._dragging:
            f0 = self._frac(*self._press)
            if f0 is None or ((x - self._press[0]) ** 2 + (y - self._press[1]) ** 2) ** 0.5 < 8:
                return
            self._dragging = True
            self._touchq.put(("down", f0))
        f = self._frac(x, y)
        if f is not None:
            self._touchq.put(("move", f))

    def on_up(self, x, y):
        nub, self._nub_hi = self._nub_hi, None
        if self._press is None:
            return
        if self._dragging:                           # finish a live finger drag
            f = self._frac(x, y) or self._frac(*self._press)
            if f:
                self._touchq.put(("up", f))
            self._dragging = False
            self._press = None
            self._do("look", self._look)
            return
        sx0, sy0 = self._press
        self._press = None
        if nub and self._hit_in("nubs", x, y) == nub:
            self._flash = (nub, time.time() + 0.2)
            self._btn(nub)
            return
        b = self._hit_in("buttons", x, y)
        if b:
            self._flash = (b, time.time() + 0.2)
            if b in ("power", "volup", "voldown", "mute"):
                self._btn(b)
            elif b == "home":
                self._do("home", self.dev.home)
            elif b == "look":
                self._do("look", self._look)
            elif b == "boxes":
                self.boxes = not self.boxes
            elif b == "drm":
                self.toggle_drm()
            return
        f1 = self._frac(sx0, sy0)
        if f1 is not None:
            self._do("tap", lambda: self._tap(f1))

    def _touch_loop(self):
        w, h = SCREEN_PX
        while True:
            items = [self._touchq.get()]
            try:
                while True:
                    items.append(self._touchq.get_nowait())
            except queue.Empty:
                pass
            out = []                                  # collapse runs of moves to the latest
            for it in items:
                if it[0] == "move" and out and out[-1][0] == "move":
                    out[-1] = it
                else:
                    out.append(it)
            for op, fr in out:
                try:
                    x, y = int(fr[0] * w), int(fr[1] * h)
                    if op == "down":
                        self.dev.touch_down(x, y)
                    elif op == "move":
                        self.dev.touch_move(x, y)
                    else:
                        self.dev.touch_up(x, y)
                except Exception:
                    pass

    def _frac(self, x, y):
        if not self._hit:
            return None
        sx, sy, SW, SH = self._hit["screen"]
        lx, ly = x - sx, y - sy
        if 0 <= lx < SW and 0 <= ly < SH:
            return lx / SW, ly / SH
        return None

    def _swipe(self, f1, f2):
        w, h = SCREEN_PX
        self.dev.swipe(int(f1[0] * w), int(f1[1] * h), int(f2[0] * w), int(f2[1] * h), 250)
        time.sleep(0.35)
        return self._look()

    def _tap(self, frac):
        if not self.elements:
            self._look()
        fx, fy = frac
        best, bd = None, 1e9
        for e in self.elements:
            ex, ey = (e.get("x") or 0) / SCREEN_PTS[0], (e.get("y") or 0) / SCREEN_PTS[1]
            dd = (fx - ex) ** 2 + (fy - ey) ** 2
            if dd < bd:
                best, bd = e, dd
        if best is None:
            return "no element near"
        same = [e for e in self.elements if e.get("label") == best.get("label")]
        idx = same.index(best) if best in same else 0
        r = self.dev.tap_label(best.get("label"), idx)
        time.sleep(0.35)
        self._look()
        return "'%s' %s" % (best.get("label"), "✓" if isinstance(r, dict) and r.get("ok") else r)

    def on_key(self, chars):
        if not chars:
            return
        if chars in ("\r", "\n"):
            t, self.typebuf = self.typebuf, ""
            if t:
                self._do("type", lambda: (self.dev.type_text(t), time.sleep(0.2), self._look())[0])
        elif chars in ("\x7f", "\b"):
            self.typebuf = self.typebuf[:-1]
        elif chars == "\x1b":
            self.typebuf = ""
        elif chars.isprintable():
            self.typebuf += chars

    def shutdown(self):
        try:
            self.stream.stop()
        except Exception:
            pass
        if self._owned is not None:
            try:
                self._owned.terminate()
            except Exception:
                pass


# ===================================================== macOS backend (AppKit) ======
def run_appkit(engine):
    import objc
    from Foundation import NSObject, NSMakeRect, NSTimer, NSBundle
    from AppKit import (
        NSApplication, NSWindow, NSView, NSImage, NSColor, NSBitmapImageRep,
        NSCalibratedRGBColorSpace, NSRectFill, NSBackingStoreBuffered,
        NSWindowStyleMaskTitled, NSWindowStyleMaskClosable, NSWindowStyleMaskResizable,
        NSWindowStyleMaskMiniaturizable, NSApplicationActivationPolicyRegular,
        NSCompositingOperationCopy, NSMenu, NSMenuItem,
    )
    try:  # make the macOS menu bar read "Mimic", not "Python"
        NSBundle.mainBundle().infoDictionary()["CFBundleName"] = "Mimic"
    except Exception:
        pass

    def pil_to_nsimage(pil):
        pil = pil.convert("RGBA")
        w, h = pil.size
        rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
            None, w, h, 8, 4, True, False, NSCalibratedRGBColorSpace, w * 4, 32)
        rep.bitmapData()[:] = pil.tobytes()
        img = NSImage.alloc().initWithSize_((w, h))
        img.addRepresentation_(rep)
        return img

    def pt(view, ev):  # Cocoa is bottom-left; flip to the top-left coords compose uses
        p = view.convertPoint_fromView_(ev.locationInWindow(), None)
        return (p.x, view.bounds().size.height - p.y)

    class MimicView(NSView):
        def initWithEngine_(self, eng):
            self = objc.super(MimicView, self).initWithFrame_(NSMakeRect(0, 0, 540, 920))
            self._eng = eng
            self._img = None
            return self

        def isFlipped(self):
            return False

        def acceptsFirstResponder(self):
            return True

        def setImage_(self, im):
            self._img = im
            self.setNeedsDisplay_(True)

        def drawRect_(self, rect):
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.04, 0.043, 0.06, 1.0).set()
            NSRectFill(self.bounds())
            if self._img is not None:
                self._img.drawInRect_fromRect_operation_fraction_(
                    self.bounds(), NSMakeRect(0, 0, 0, 0), NSCompositingOperationCopy, 1.0)

        def mouseDown_(self, ev):
            self._eng.on_down(*pt(self, ev))

        def mouseDragged_(self, ev):
            self._eng.on_move(*pt(self, ev))

        def mouseUp_(self, ev):
            self._eng.on_up(*pt(self, ev))

        def keyDown_(self, ev):
            self._eng.on_key(ev.characters())

    class Pump(NSObject):
        def initWith_view_(self, eng, view):
            self = objc.super(Pump, self).init()
            self._eng, self._view, self._last = eng, view, None
            return self

        def tick_(self, timer):
            b = self._view.bounds()
            self._eng.wh = (int(b.size.width), int(b.size.height))
            pil = self._eng.next_pil
            if pil is not None and id(pil) != self._last:
                self._last = id(pil)
                self._view.setImage_(pil_to_nsimage(pil))

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    # proper app menu (Mimic ▸ Hide / Quit) instead of the bare default
    menubar = NSMenu.alloc().init()
    appmi = NSMenuItem.alloc().init()
    menubar.addItem_(appmi)
    app.setMainMenu_(menubar)
    appmenu = NSMenu.alloc().init()
    appmenu.addItemWithTitle_action_keyEquivalent_("Hide Mimic", "hide:", "h")
    appmenu.addItem_(NSMenuItem.separatorItem())
    appmenu.addItemWithTitle_action_keyEquivalent_("Quit Mimic", "terminate:", "q")
    appmi.setSubmenu_(appmenu)
    icon = os.path.join(os.path.dirname(__file__), "icon.png")
    if os.path.exists(icon):
        try:
            app.setApplicationIconImage_(NSImage.alloc().initWithContentsOfFile_(icon))
        except Exception:
            pass
    style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
             NSWindowStyleMaskResizable | NSWindowStyleMaskMiniaturizable)
    win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(200, 100, 540, 920), style, NSBackingStoreBuffered, False)
    win.setTitle_("Mimic")
    win.setMinSize_(NSMakeRect(0, 0, 470, 700).size)
    view = MimicView.alloc().initWithEngine_(engine)
    win.setContentView_(view)
    win.makeFirstResponder_(view)
    win.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)
    pump = Pump.alloc().initWith_view_(engine, view)
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(0.016, pump, "tick:", None, True)
    app.run()


# ================================================ Windows / Linux backend (Tk) =====
def run_tk(engine):
    import tkinter as tk
    from PIL import ImageTk

    root = tk.Tk()
    root.title("Mimic")
    root.configure(bg="#0a0b0f")
    root.geometry("540x920")
    root.minsize(470, 700)
    try:
        icon = os.path.join(os.path.dirname(__file__), "icon.png")
        if os.path.exists(icon):
            root.iconphoto(True, ImageTk.PhotoImage(Image.open(icon)))
    except Exception:
        pass
    canvas = tk.Canvas(root, bg="#0a0b0f", highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    canvas.bind("<ButtonPress-1>", lambda e: engine.on_down(e.x, e.y))
    canvas.bind("<B1-Motion>", lambda e: engine.on_move(e.x, e.y))
    canvas.bind("<ButtonRelease-1>", lambda e: engine.on_up(e.x, e.y))
    root.bind("<Key>", lambda e: engine.on_key(e.char if e.char else ""))
    state = {"last": None, "img": None}

    def tick():
        engine.wh = (canvas.winfo_width(), canvas.winfo_height())
        pil = engine.next_pil
        if pil is not None and id(pil) != state["last"]:
            state["last"] = id(pil)
            state["img"] = ImageTk.PhotoImage(pil)
            canvas.delete("all")
            canvas.create_image(0, 0, anchor="nw", image=state["img"])
        root.after(16, tick)

    root.protocol("WM_DELETE_WINDOW", lambda: (engine.shutdown(), root.destroy()))
    root.after(16, tick)
    root.mainloop()


def main():
    engine = Engine()
    if sys.platform == "darwin":
        run_appkit(engine)
    else:
        run_tk(engine)


if __name__ == "__main__":
    main()
