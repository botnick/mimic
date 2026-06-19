#!/usr/bin/env python3
"""Mimic iOS live viewer — a premium, scrcpy-style NATIVE window for the iPhone.

A native macOS (Cocoa / AppKit) window — no browser — that mirrors the jailbroken
device LIVE over USB and drives it, reusing the proven control model in
mimic/ios/device.py:

    * live screen   : go-ios MJPEG stream (full-res JPEG frames over the USB DVT channel)
    * click          : nearest accessibility element from look() -> tap_label()
    * drag           : swipe() (pixel coords)
    * side buttons   : clickable nubs on the device frame -> button() (Consumer-HID)
    * header buttons : Home / Look / A11y
    * type           : just type while the window is focused; Enter sends -> type_text()

The whole window — device body, rounded screen, side buttons, header — is composited with
Pillow into one image per frame and drawn into a single NSView; mouse clicks are hit-tested
against that layout. We use AppKit instead of Tkinter because macOS ships a broken Tk 8.5
with the system Python (blank windows / frozen event loop); Cocoa's run loop is solid.

Why click->nearest-label and not a raw pixel touch: discrete IOHIDEvent taps do not fire
gesture recognizers on this device (see device.py), so taps go through the a11y tree.

Run:  python3 -m mimic.ios.viewer      (needs: pip install pyobjc-framework-Cocoa pillow)
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

import objc
from Foundation import NSData, NSObject, NSMakeRect, NSTimer
from AppKit import (
    NSApplication, NSWindow, NSView, NSImage, NSColor, NSApp,
    NSBackingStoreBuffered, NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
    NSWindowStyleMaskResizable, NSWindowStyleMaskMiniaturizable,
    NSApplicationActivationPolicyRegular, NSCompositingOperationCopy,
    NSBitmapImageRep, NSCalibratedRGBColorSpace,
)

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
    paths = (["/System/Library/Fonts/SFNSDisplay.ttf", "/System/Library/Fonts/SFNS.ttf",
              "/Library/Fonts/Arial Bold.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"]
             if bold else
             ["/System/Library/Fonts/SFNS.ttf", "/Library/Fonts/Arial.ttf",
              "/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Helvetica.ttc"])
    for p in paths:
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
        cy = py + b + int(SH * cyf)
        if side == "L":
            x1, x2 = px - nub_w, px + int(b * 0.4)
        else:
            x1, x2 = px + PW - int(b * 0.4), px + PW + nub_w
        rr(d, [x1, cy, x2, cy + ln], nub_w, fill=P["nub"] + (255,), outline=P["nub_edge"] + (255,), width=1)
        nubs[name] = (min(x1, x2) - 4, cy - 3, max(x1, x2) + 4, cy + ln + 3)

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


# ----------------------------------------------------------------- full UI compose
_CHROME = {}  # cache the heavy device chrome (shadow/blur) per window size


def compose(W, H, frame, *, status, conn, boxes_on, nub_hi, typebuf, fps=0, ms=0):
    img = Image.new("RGBA", (W, H), P["bg"] + (255,))
    d = ImageDraw.Draw(img)
    # header
    cy = HEADER_H // 2
    d.rectangle([0, 0, W, HEADER_H], fill=P["panel"] + (255,))
    cc = {"live": P["green"], "connecting": P["amber"], "error": P["red"]}[conn]
    d.ellipse([16, cy - 5, 26, cy + 5], fill=cc + (255,))
    d.text((36, cy - 10), "Mimic", font=F_TITLE, fill=P["text"] + (255,))
    d.text((110, cy - 7), "%d fps · %d ms" % (fps, ms), font=F_MONO, fill=P["green"] + (255,))
    msg = typebuf and ("⌨ " + typebuf) or status
    d.text((220, cy - 7), msg[:28], font=F_MONO, fill=(P["accent"] if typebuf else P["dim"]) + (255,))

    # header buttons (right): Home, Look, A11y
    btns = {}
    bw, bh, gap = 40, 30, 8
    defs = [("home", _ic_home, P["accent"]), ("look", _ic_look, P["accent"]),
            ("boxes", _ic_boxes, P["green"] if boxes_on else P["icon"])]
    bx = W - len(defs) * (bw + gap) - 6
    for name, icon, col in defs:
        fill = P["btn_on"] if (name == "boxes" and boxes_on) else P["btn"]
        rr(d, [bx, 8, bx + bw, 8 + bh], 9, fill=fill + (255,))
        icon(d, bx + bw // 2, 8 + bh // 2, (P["text"] if (name == "boxes" and boxes_on) else col))
        btns[name] = (bx, 8, bx + bw, 8 + bh)
        bx += bw + gap

    # phone stage below header — build_chrome is heavy (GaussianBlur), so cache + copy
    sw, sh = W, H - HEADER_H
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

    # hitmap in window coords
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
    if _port_open(port):
        return None
    proc = subprocess.Popen([GOIOS, "screenshot", "--stream", "--port=%d" % port],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(20):
        if _port_open(port):
            break
        time.sleep(0.3)
    return proc


def pil_to_nsimage(pil):
    # write raw RGBA straight into an NSBitmapImageRep — ~13x faster than PNG-encoding
    pil = pil.convert("RGBA")
    w, h = pil.size
    raw = pil.tobytes()
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, w, h, 8, 4, True, False, NSCalibratedRGBColorSpace, w * 4, 32)
    rep.bitmapData()[:] = raw
    img = NSImage.alloc().initWithSize_((w, h))
    img.addRepresentation_(rep)
    return img


# ------------------------------------------------------------------- AppKit window
def _point(view, ev):
    # view is NOT flipped (default bottom-left origin) so the NSImage draws upright;
    # convert the click to the top-left coords our PIL hitmap uses.
    p = view.convertPoint_fromView_(ev.locationInWindow(), None)
    return (p.x, view.bounds().size.height - p.y)


class MimicView(NSView):
    def initWithCtl_(self, ctl):
        self = objc.super(MimicView, self).initWithFrame_(NSMakeRect(0, 0, 486, 900))
        self._ctl = ctl
        self._img = None
        return self

    def isFlipped(self):
        return False

    def acceptsFirstResponder(self):
        return True

    def setImage_(self, nsimg):
        self._img = nsimg
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        from AppKit import NSRectFill
        NSColor.colorWithCalibratedRed_green_blue_alpha_(0.04, 0.043, 0.06, 1.0).set()
        NSRectFill(self.bounds())
        if self._img is not None:
            self._img.drawInRect_fromRect_operation_fraction_(
                self.bounds(), NSMakeRect(0, 0, 0, 0), NSCompositingOperationCopy, 1.0)

    def mouseDown_(self, ev):
        self._ctl.on_down(*_point(self, ev))

    def mouseUp_(self, ev):
        self._ctl.on_up(*_point(self, ev))

    def keyDown_(self, ev):
        self._ctl.on_key(ev.characters())


class _Timer(NSObject):
    def initWithCtl_(self, ctl):
        self = objc.super(_Timer, self).init()
        self._ctl = ctl
        return self

    def tick_(self, timer):
        try:
            self._ctl.tick()
        except Exception:
            pass


class Controller:
    def __init__(self):
        self.dev = IOSDevice()
        self.elements, self.boxes = [], False
        self.status, self.conn = "starting…", "connecting"
        self.typebuf = ""
        self._jobs = queue.Queue()
        self._press = None
        self._nub_hi = None
        self._hit = None
        self._owned = None
        self.view = None
        self.stream = MJPEGStream(STREAM_URL)
        self._wh = (0, 0)
        self._next_img = None
        self.fps, self.lat_ms = 0, 0
        threading.Thread(target=self._boot, daemon=True).start()
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._render_loop, daemon=True).start()

    # ---- worker plumbing ----
    def _set(self, s):
        self.status = s

    def _do(self, name, fn):
        self._jobs.put((name, fn))

    def _worker(self):
        while True:
            name, fn = self._jobs.get()
            try:
                r = fn()
                self._set("%s · %s" % (name, r if isinstance(r, (str, dict)) else "ok"))
            except Exception as e:  # noqa: BLE001
                self._set("✗ %s: %s" % (name, e))

    def _boot(self):
        self._set("starting stream…")
        self._owned = ensure_stream_server()
        self.stream.start()
        self._set("connecting frida…")
        try:
            self.dev.ensure_frida()
            self._look()
            self._set("ready")
        except Exception as e:  # noqa: BLE001
            self._set("frida error: %s" % e)

    def _look(self):
        r = self.dev.look()
        self.elements = r.get("elements", []) if isinstance(r, dict) else []
        app = r.get("app") if isinstance(r, dict) else "?"
        self._set("look: %s · %d el" % (app, len(self.elements)))
        return app

    # ---- main-thread timer: stays CHEAP so clicks are instant ----
    def tick(self):
        if self.view is None:
            return
        b = self.view.bounds()
        self._wh = (int(b.size.width), int(b.size.height))
        img = self._next_img
        if img is not None:
            self._next_img = None
            self.view.setImage_(img)

    # ---- background render thread: heavy PIL/NSImage work, as fast as frames arrive ----
    def _render_loop(self):
        last_id, last_ui, last_draw = None, None, 0.0
        cnt, t_fps = 0, time.time()
        while True:
            W, H = self._wh
            if W < 40 or H < 40:
                time.sleep(0.02)
                continue
            self.conn = ("live" if self.stream.connected
                         else "error" if self.stream.error else "connecting")
            cur = self.stream.latest
            now = time.time()
            ui = (self.status, self._nub_hi, self.boxes, self.typebuf, self.conn)
            if id(cur) == last_id and ui == last_ui and now - last_draw < 0.3:
                time.sleep(0.003)                   # idle: nothing new to draw
                continue
            last_id, last_ui, last_draw = id(cur), ui, now
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
                                   typebuf=self.typebuf, fps=self.fps, ms=self.lat_ms)
                self._hit = hit
                self._next_img = pil_to_nsimage(img)
            except Exception:
                continue
            self.lat_ms = int(self.lat_ms * 0.7 + (time.time() - t0) * 1000 * 0.3)
            cnt += 1
            if now - t_fps >= 0.5:
                self.fps = int(round(cnt / (now - t_fps)))
                cnt, t_fps = 0, now

    # ---- input ----
    def _btn(self, name):
        self._do(name, lambda: self.dev.button(name))

    def _hit_nub(self, x, y):
        if not self._hit:
            return None
        for n, (x1, y1, x2, y2) in self._hit["nubs"].items():
            if x1 <= x <= x2 and y1 <= y <= y2:
                return n
        return None

    def _hit_btn(self, x, y):
        if not self._hit:
            return None
        for n, (x1, y1, x2, y2) in self._hit["buttons"].items():
            if x1 <= x <= x2 and y1 <= y <= y2:
                return n
        return None

    def on_down(self, x, y):
        self._press = (x, y)
        self._nub_hi = self._hit_nub(x, y)

    def on_up(self, x, y):
        nub, self._nub_hi = self._nub_hi, None
        if self._press is None:
            return
        sx0, sy0 = self._press
        self._press = None
        if nub and self._hit_nub(x, y) == nub:
            self._btn(nub)
            return
        b = self._hit_btn(x, y)
        if b == "home":
            self._do("home", self.dev.home); return
        if b == "look":
            self._do("look", self._look); return
        if b == "boxes":
            self.boxes = not self.boxes; return
        f1, f2 = self._frac(sx0, sy0), self._frac(x, y)
        if f1 is None or f2 is None:
            return
        if ((x - sx0) ** 2 + (y - sy0) ** 2) ** 0.5 > 18:
            self._do("swipe", lambda: self._swipe(f1, f2))
        else:
            self._do("tap", lambda: self._tap(f1))

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


def main():
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    _icon = os.path.join(os.path.dirname(__file__), "icon.png")
    if os.path.exists(_icon):
        try:
            app.setApplicationIconImage_(NSImage.alloc().initWithContentsOfFile_(_icon))
        except Exception:
            pass
    ctl = Controller()
    timer_target = _Timer.alloc().initWithCtl_(ctl)
    style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
             NSWindowStyleMaskResizable | NSWindowStyleMaskMiniaturizable)
    win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(200, 120, 486, 900), style, NSBackingStoreBuffered, False)
    win.setTitle_("Mimic")
    win.setMinSize_(NSMakeRect(0, 0, 380, 680).size)
    view = MimicView.alloc().initWithCtl_(ctl)
    ctl.view = view
    win.setContentView_(view)
    win.makeFirstResponder_(view)
    win.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        0.016, timer_target, "tick:", None, True)
    app.run()


if __name__ == "__main__":
    main()
