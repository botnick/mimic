#!/usr/bin/env python3
"""Mimic iOS live viewer — a premium, scrcpy-style native window for the iPhone.

A native Tk window (no browser) that mirrors the jailbroken device LIVE over USB and
drives it, reusing the proven control model in mimic/ios/device.py:

    * live screen   : go-ios MJPEG stream (full-res JPEG frames over the USB DVT channel)
    * click          : nearest accessibility element from look() -> tap_label()
    * drag           : swipe() (pixel coords)
    * side buttons   : clickable nubs on the device frame -> button() (Consumer-HID)
    * rail buttons   : Lock / Vol+ / Vol- / Mute / Home / Look / A11y
    * text           : type_text() into the focused field

The phone — body, rounded screen, side buttons, earpiece — is composited with PIL into
one image, so it looks like a real device and avoids Tk's inability to style native
buttons on macOS. The static chrome is cached per window size; only the screen is pasted
per frame.

Why click->nearest-label and not a raw pixel touch: discrete IOHIDEvent taps do not fire
gesture recognizers on this device (see device.py), so taps go through the a11y tree.
Custom-drawn views with no a11y element (games, the calculator keypad) are the known wall.
Scrolling/dragging uses synthetic touch, which does work; hardware keys use Consumer-HID.

Run:
    python3 -m mimic.ios.viewer
    python3 mimic/ios/viewer.py
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
import tkinter as tk
import tkinter.font as tkfont
from urllib.request import urlopen

from PIL import Image, ImageDraw, ImageFilter, ImageTk

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from mimic.ios.device import IOSDevice, GOIOS  # noqa: E402

SCREEN_PTS = (375, 667)        # look() element centres are in POINTS
SCREEN_PX = (750, 1334)        # swipe() takes PIXELS
STREAM_PORT = 3333
STREAM_URL = "http://127.0.0.1:%d/" % STREAM_PORT

P = {
    "bg": "#0a0b0f", "body": "#0e0f15", "body_edge": "#3a3f4b", "bevel": "#20242e",
    "screen_bg": "#000000", "nub": "#171a21", "nub_edge": "#333845",
    "panel": "#121420", "btn": "#1c1f2b", "btn_h": "#262a39", "btn_a": "#333a4f",
    "accent": "#0a84ff", "accent_h": "#3a9bff", "green": "#30d158",
    "amber": "#ff9f0a", "red": "#ff453a", "text": "#f2f3f7", "dim": "#878d9e",
    "icon": "#d6dae6",
}


def _h(c):
    c = c.lstrip("#")
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))


def _pick_font(prefs):
    fams = set(tkfont.families())
    for p in prefs:
        if p in fams:
            return p
    return prefs[-1]


def round_rect(cv, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return cv.create_polygon(pts, smooth=True, **kw)


# ============================================================== device-frame render
def build_chrome(W, H):
    """Build the static device chrome (body, screen recess, side buttons, earpiece)
    once per window size. Returns (chrome RGBA, geom dict, rounded screen mask)."""
    sw0, sh0 = SCREEN_PX
    bez = 0.035 * sw0
    pw0, ph0 = sw0 + 2 * bez, sh0 + 2 * bez
    pad = 20
    scale = min((W - 2 * pad) / pw0, (H - 2 * pad) / (ph0 * 1.06))
    PW, PH = int(pw0 * scale), int(ph0 * scale)
    SW, SH = int(sw0 * scale), int(sh0 * scale)
    b = int(bez * scale)
    body_r = int(PW * 0.11)
    scr_r = max(6, body_r - b)
    px, py = (W - PW) // 2, (H - PH) // 2
    sx, sy = px + b, py + b

    img = Image.new("RGBA", (W, H), _h(P["bg"]) + (255,))

    # soft drop shadow
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle(
        [px, py + int(PH * 0.03), px + PW, py + PH + int(PH * 0.03)],
        radius=body_r, fill=(0, 0, 0, 160))
    img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(max(6, int(PW * 0.05)))))

    d = ImageDraw.Draw(img)

    # side buttons (drawn before the body so they read as protruding)
    nub_w = max(3, int(b * 0.46))
    nubs = {}

    def nub(name, side, cy_frac, len_frac):
        ln = int(SH * len_frac)
        cy = py + b + int(SH * cy_frac)
        if side == "L":
            x1, x2 = px - nub_w, px + int(b * 0.4)
        else:
            x1, x2 = px + PW - int(b * 0.4), px + PW + nub_w
        d.rounded_rectangle([x1, cy, x2, cy + ln], radius=nub_w,
                            fill=_h(P["nub"]) + (255,), outline=_h(P["nub_edge"]) + (255,), width=1)
        nubs[name] = (min(x1, x2) - 4, cy - 3, max(x1, x2) + 4, cy + ln + 3)  # padded hit box

    nub("power", "R", 0.17, 0.13)
    nub("mute", "L", 0.10, 0.05)
    nub("volup", "L", 0.205, 0.11)
    nub("voldown", "L", 0.345, 0.11)

    # body + subtle inner bevel
    d.rounded_rectangle([px, py, px + PW, py + PH], radius=body_r,
                        fill=_h(P["body"]) + (255,), outline=_h(P["body_edge"]) + (255,), width=2)
    d.rounded_rectangle([px + 2, py + 2, px + PW - 2, py + PH - 2], radius=body_r - 2,
                        outline=_h(P["bevel"]) + (150,), width=1)
    # screen recess
    d.rounded_rectangle([sx, sy, sx + SW, sy + SH], radius=scr_r, fill=_h(P["screen_bg"]) + (255,))
    # earpiece slit + camera dot
    slit_w, slit_h = int(SW * 0.16), max(3, int(b * 0.22))
    slx, sly = px + (PW - slit_w) // 2, py + (b - slit_h) // 2
    d.rounded_rectangle([slx, sly, slx + slit_w, sly + slit_h], radius=slit_h // 2, fill=(40, 44, 54, 255))
    cam = max(2, int(b * 0.13))
    d.ellipse([slx + slit_w + int(b * 0.45), sly, slx + slit_w + int(b * 0.45) + cam, sly + cam],
              fill=(28, 32, 40, 255))

    mask = Image.new("L", (SW, SH), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, SW, SH], radius=scr_r, fill=255)
    geom = {"phone": (px, py, PW, PH), "screen": (sx, sy, SW, SH), "nubs": nubs}
    return img, geom, mask


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


# ---------------------------------------------------------------- vector icon glyphs
def icon_power(cv, x, y, c):
    cv.create_arc(x - 9, y - 9, x + 9, y + 9, start=72, extent=-324, style="arc", outline=c, width=2)
    cv.create_line(x, y - 11, x, y - 1, fill=c, width=2, capstyle="round")


def icon_plus(cv, x, y, c):
    cv.create_line(x - 7, y, x + 7, y, fill=c, width=2, capstyle="round")
    cv.create_line(x, y - 7, x, y + 7, fill=c, width=2, capstyle="round")


def icon_minus(cv, x, y, c):
    cv.create_line(x - 7, y, x + 7, y, fill=c, width=2, capstyle="round")


def icon_mute(cv, x, y, c):
    cv.create_polygon(x - 8, y - 3, x - 4, y - 3, x, y - 7, x, y + 7, x - 4, y + 3, x - 8, y + 3,
                      fill=c, outline=c)
    cv.create_line(x + 2, y - 7, x + 9, y + 7, fill=P["red"], width=2, capstyle="round")


def icon_home(cv, x, y, c):
    cv.create_oval(x - 8, y - 8, x + 8, y + 8, outline=c, width=2)
    cv.create_rectangle(x - 3, y - 3, x + 3, y + 3, outline=c, width=2)


def icon_look(cv, x, y, c):
    cv.create_arc(x - 8, y - 8, x + 8, y + 8, start=40, extent=290, style="arc", outline=c, width=2)
    cv.create_polygon(x + 7, y - 9, x + 11, y - 3, x + 3, y - 4, fill=c, outline=c)


def icon_boxes(cv, x, y, c):
    for dx, dy in ((-8, -8), (1, -8), (-8, 1), (1, 1)):
        cv.create_rectangle(x + dx, y + dy, x + dx + 7, y + dy + 7, outline=c, width=2)


# ------------------------------------------------------------------- rail icon button
class IconButton(tk.Canvas):
    def __init__(self, parent, label, command, icon, w=72, h=56,
                 accent=None, toggle=False, font="Helvetica"):
        super().__init__(parent, width=w, height=h, bg=P["panel"],
                         highlightthickness=0, bd=0, cursor="pointinghand")
        self.cmd, self.icon, self.label = command, icon, label
        self.w, self.h, self.accent, self.font = w, h, accent, font
        self.toggle, self.on, self._st = toggle, False, "base"
        self.bind("<Enter>", lambda e: self._set("hover"))
        self.bind("<Leave>", lambda e: self._set("base"))
        self.bind("<ButtonPress-1>", lambda e: self._set("active"))
        self.bind("<ButtonRelease-1>", self._release)
        self._render()

    def _set(self, s):
        self._st = s
        self._render()

    def _release(self, ev):
        inside = 0 <= ev.x <= self.w and 0 <= ev.y <= self.h
        self._set("hover" if inside else "base")
        if inside and self.cmd:
            if self.toggle:
                self.on = not self.on
            self.cmd()

    def _render(self):
        self.delete("all")
        fill = (self.accent or P["accent"]) if (self.toggle and self.on) else \
            {"base": P["btn"], "hover": P["btn_h"], "active": P["btn_a"]}[self._st]
        round_rect(self, 3, 3, self.w - 3, self.h - 3, 15, fill=fill, outline="")
        col = P["text"] if (self.toggle and self.on) else (self.accent or P["icon"])
        self.icon(self, self.w / 2, self.h / 2 - 6, col)
        self.create_text(self.w / 2, self.h - 11, text=self.label,
                         fill=P["text"] if (self.toggle and self.on) else P["dim"],
                         font=(self.font, 9, "bold"))


# ----------------------------------------------------------------------------- app
class Viewer(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Mimic")
        self.configure(bg=P["bg"])
        self.geometry("486x940")
        self.minsize(400, 720)
        self.F = _pick_font(["SF Pro Text", "SF Pro Display", "Helvetica Neue", "Helvetica"])
        self.FM = _pick_font(["SF Mono", "Menlo", "Monaco", "Courier"])

        self.dev = IOSDevice()
        self.elements, self.show_boxes = [], False
        self._statusq, self._jobs = queue.Queue(), queue.Queue()
        self._conn = "connecting"
        self._chrome = self._geom = self._mask = None
        self._stage_size = (0, 0)
        self._press = None
        self._nub_hi = None
        self._tkimg = None
        self._owned = None

        self._build_ui()
        # everything that can block goes on threads so the window paints immediately
        self.stream = MJPEGStream(STREAM_URL)
        threading.Thread(target=self._boot, daemon=True).start()
        threading.Thread(target=self._worker, daemon=True).start()
        self.after(33, self._tick)
        self.protocol("WM_DELETE_WINDOW", self._close)

    # ------------------------------------------------------------------- ui build
    def _build_ui(self):
        head = tk.Frame(self, bg=P["panel"], height=50)
        head.pack(side="top", fill="x")
        head.pack_propagate(False)
        self._dotcv = tk.Canvas(head, width=12, height=12, bg=P["panel"], highlightthickness=0)
        self._dotcv.pack(side="left", padx=(16, 8))
        self._dot = self._dotcv.create_oval(2, 2, 10, 10, fill=P["amber"], outline="")
        tk.Label(head, text="Mimic", bg=P["panel"], fg=P["text"],
                 font=(self.F, 16, "bold")).pack(side="left")
        self.pill = tk.Canvas(head, width=86, height=24, bg=P["panel"], highlightthickness=0)
        self.pill.pack(side="right", padx=14)

        body = tk.Frame(self, bg=P["bg"])
        body.pack(side="top", fill="both", expand=True)

        rail = tk.Frame(body, bg=P["panel"], width=92)
        rail.pack(side="right", fill="y")
        rail.pack_propagate(False)
        tk.Frame(rail, bg=P["panel"], height=6).pack()
        rail_defs = [
            ("LOCK", lambda: self._btn("power"), icon_power, P["red"], False),
            ("VOL", lambda: self._btn("volup"), icon_plus, None, False),
            ("VOL", lambda: self._btn("voldown"), icon_minus, None, False),
            ("MUTE", lambda: self._btn("mute"), icon_mute, None, False),
            ("HOME", lambda: self._do("home", self.dev.home), icon_home, P["accent"], False),
            ("LOOK", lambda: self._do("look", self._look), icon_look, P["accent"], False),
        ]
        for label, cmd, icon, acc, tog in rail_defs:
            IconButton(rail, label, cmd, icon, accent=acc, font=self.F).pack(pady=4, padx=10)
        self._boxes_btn = IconButton(rail, "A11Y", self._toggle_boxes, icon_boxes,
                                     accent=P["green"], toggle=True, font=self.F)
        self._boxes_btn.pack(pady=4, padx=10)

        self.stage = tk.Canvas(body, bg=P["bg"], highlightthickness=0)
        self.stage.pack(side="left", fill="both", expand=True)
        self.stage.bind("<ButtonPress-1>", self._press_stage)
        self.stage.bind("<ButtonRelease-1>", self._release_stage)

        bar = tk.Frame(self, bg=P["panel"], height=56)
        bar.pack(side="bottom", fill="x")
        bar.pack_propagate(False)
        wrap = tk.Frame(bar, bg=P["btn"])
        wrap.pack(side="left", fill="x", expand=True, padx=(14, 8), pady=10)
        self.entry = tk.Entry(wrap, bg=P["btn"], fg=P["text"], insertbackground=P["accent"],
                              relief="flat", font=(self.F, 13))
        self.entry.pack(side="left", fill="x", expand=True, padx=12, pady=7)
        self.entry.bind("<Return>", lambda e: self._send_text())
        send = tk.Canvas(bar, width=56, height=34, bg=P["panel"], highlightthickness=0,
                         cursor="pointinghand")
        send.pack(side="right", padx=(0, 14))
        round_rect(send, 1, 1, 55, 33, 12, fill=P["accent"], outline="")
        send.create_text(28, 17, text="Type", fill="white", font=(self.F, 12, "bold"))
        send.bind("<Button-1>", lambda e: self._send_text())

        self.status = tk.Label(self, text="starting…", anchor="w", bg=P["bg"], fg=P["dim"],
                               font=(self.FM, 10))
        self.status.pack(side="bottom", fill="x", padx=14, pady=(0, 4))
        self._paint_pill()

    # --------------------------------------------------------------- status helpers
    def _set_status(self, s):
        self._statusq.put(s)

    def _paint_pill(self):
        c = {"live": P["green"], "connecting": P["amber"], "error": P["red"]}[self._conn]
        txt = {"live": "● live", "connecting": "● link…", "error": "● off"}[self._conn]
        self.pill.delete("all")
        round_rect(self.pill, 1, 1, 84, 22, 11, fill=P["btn"], outline="")
        self.pill.create_text(43, 12, text=txt, fill=c, font=(self.F, 11, "bold"))
        self._dotcv.itemconfig(self._dot, fill=c)

    # ------------------------------------------------------------- worker plumbing
    def _do(self, name, fn):
        self._jobs.put((name, fn))

    def _btn(self, name):
        self._do(name, lambda: self.dev.button(name))

    def _worker(self):
        while True:
            name, fn = self._jobs.get()
            try:
                r = fn()
                self._set_status("%s · %s" % (name, r if isinstance(r, (str, dict)) else "ok"))
            except Exception as e:  # noqa: BLE001
                self._set_status("✗ %s: %s" % (name, e))

    def _boot(self):
        self._set_status("starting stream…")
        self._owned = ensure_stream_server()
        self.stream.start()
        self._set_status("connecting frida…")
        try:
            self.dev.ensure_frida()
            self._look()
            self._set_status("ready")
        except Exception as e:  # noqa: BLE001
            self._set_status("frida error: %s" % e)

    def _look(self):
        r = self.dev.look()
        self.elements = r.get("elements", []) if isinstance(r, dict) else []
        app = r.get("app") if isinstance(r, dict) else "?"
        self._set_status("look: %s · %d elements" % (app, len(self.elements)))
        return app

    def _toggle_boxes(self):
        self.show_boxes = self._boxes_btn.on

    def _send_text(self):
        t = self.entry.get()
        if not t:
            return
        self.entry.delete(0, "end")
        self._do("type", lambda: (self.dev.type_text(t), time.sleep(0.2), self._look())[0])

    # --------------------------------------------------------------- render loop
    def _tick(self):
        try:
            while True:
                self.status.config(text=self._statusq.get_nowait())
        except queue.Empty:
            pass
        new_conn = "live" if self.stream.connected else ("error" if self.stream.error else "connecting")
        if new_conn != self._conn:
            self._conn = new_conn
            self._paint_pill()

        cw, ch = self.stage.winfo_width(), self.stage.winfo_height()
        if cw > 8 and ch > 8:
            if (cw, ch) != self._stage_size:
                self._stage_size = (cw, ch)
                self._chrome, self._geom, self._mask = build_chrome(cw, ch)
            self._render()
        self.after(33, self._tick)

    def _render(self):
        img = self._chrome.copy()
        sx, sy, SW, SH = self._geom["screen"]
        if self.stream.latest:
            try:
                fr = Image.open(io.BytesIO(self.stream.latest)).convert("RGB").resize((SW, SH))
                if self.show_boxes and self.elements:
                    self._overlay(fr)
                img.paste(fr, (sx, sy), self._mask)
            except Exception:
                pass
        if self._nub_hi and self._nub_hi in self._geom["nubs"]:
            x1, y1, x2, y2 = self._geom["nubs"][self._nub_hi]
            ImageDraw.Draw(img).rounded_rectangle([x1 + 4, y1 + 3, x2 - 4, y2 - 3],
                                                  radius=4, outline=_h(P["accent"]) + (255,), width=2)
        self._tkimg = ImageTk.PhotoImage(img)
        self.stage.delete("all")
        self.stage.create_image(0, 0, anchor="nw", image=self._tkimg)

    def _overlay(self, img):
        d = ImageDraw.Draw(img)
        iw, ih = img.size
        kx, ky = iw / SCREEN_PTS[0], ih / SCREEN_PTS[1]
        for e in self.elements:
            x, y = (e.get("x") or 0) * kx, (e.get("y") or 0) * ky
            d.ellipse([x - 5, y - 5, x + 5, y + 5], outline=P["green"], width=2)

    # -------------------------------------------------------------------- input
    def _hit_nub(self, x, y):
        if not self._geom:
            return None
        for name, (x1, y1, x2, y2) in self._geom["nubs"].items():
            if x1 <= x <= x2 and y1 <= y <= y2:
                return name
        return None

    def _press_stage(self, ev):
        self._press = (ev.x, ev.y)
        n = self._hit_nub(ev.x, ev.y)
        if n:
            self._nub_hi = n

    def _release_stage(self, ev):
        nub, self._nub_hi = self._nub_hi, None
        if self._press is None:
            return
        sx0, sy0 = self._press
        self._press = None
        if nub and self._hit_nub(ev.x, ev.y) == nub:
            self._btn(nub)
            return
        f1, f2 = self._to_frac(sx0, sy0), self._to_frac(ev.x, ev.y)
        if f1 is None or f2 is None:
            return
        if ((ev.x - sx0) ** 2 + (ev.y - sy0) ** 2) ** 0.5 > 22:
            self._do("swipe", lambda: self._swipe(f1, f2))
        else:
            self._do("tap", lambda: self._tap(f1))

    def _to_frac(self, ex, ey):
        if not self._geom:
            return None
        sx, sy, SW, SH = self._geom["screen"]
        lx, ly = ex - sx, ey - sy
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
            return "no a11y element near"
        same = [e for e in self.elements if e.get("label") == best.get("label")]
        idx = same.index(best) if best in same else 0
        r = self.dev.tap_label(best.get("label"), idx)
        time.sleep(0.35)
        self._look()
        return "'%s' %s" % (best.get("label"), "✓" if isinstance(r, dict) and r.get("ok") else r)

    # -------------------------------------------------------------------- teardown
    def _close(self):
        try:
            self.stream.stop()
        except Exception:
            pass
        if self._owned is not None:
            try:
                self._owned.terminate()
            except Exception:
                pass
        self.destroy()


def main():
    Viewer().mainloop()


if __name__ == "__main__":
    main()
