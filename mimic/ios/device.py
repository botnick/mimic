"""Mimic iOS device controller — the PROVEN control model.

Hybrid stack (validated on iPhone 8 / iOS 16.7.16 / palera1n rootless):
  * go-ios (DDI mounted)  -> launch apps, screenshot, list apps   (no frida needed)
  * frida-server          -> a11y element tree (ui), activate (sendActions /
                             accessibilityActivate), set_text, swipe/scroll,
                             home / wake / unlock / keep-awake (SpringBoard inject)

Why this model: raw IOHIDEvent *taps* do NOT fire gesture recognizers on this
device (pan/scroll works, discrete tap does not), and WebDriverAgent cannot be
launched (dev services reject the AppSync-sideloaded runner). The accessibility
path — find an element by label and fire its action — DOES work and is how this
driver "taps". It is per-app injection, so frida-hardened apps need the gadget
bypass (see README); ordinary apps work directly.

frida-server here is started/kept-alive by this module (foreground over a held
SSH session) so it survives without a persistent launchd daemon.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import time
from typing import Any, Optional

import frida

HERE = os.path.dirname(__file__)
AGENT_JS = os.path.join(HERE, "agent.js")
TEL_JS = os.path.join(HERE, "tel.js")
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))

# --- host tooling paths + per-device config -------------------------------
# Values come from mimic.config.json (written by scripts/setup.sh) so the SAME
# code adapts to any jailbroken device; the defaults match a palera1n rootless
# iPhone reached over USB.
def _find_iproxy() -> str:
    import shutil
    for p in ("~/homebrew/bin/iproxy", "/opt/homebrew/bin/iproxy", "/usr/local/bin/iproxy"):
        pp = os.path.expanduser(p)
        if os.path.exists(pp):
            return pp
    return shutil.which("iproxy") or "iproxy"


def _load_config() -> dict:
    cfg = {"iproxy": _find_iproxy(), "frida_port": 27042, "ssh_local_port": 44022,
           "ssh_device_port": 44, "ssh_pw": "alpine", "jb_prefix": "/var/jb",
           "gadget_port": 27052}
    for p in (os.path.join(ROOT, "mimic.config.json"),
              os.path.expanduser("~/.mimic/config.json")):
        try:
            with open(p) as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg


_CFG = _load_config()
IPROXY = _CFG["iproxy"]
GOIOS = os.path.join(ROOT, "tools", "goios", "ios")          # launch/screenshot/apps
SSH_PW = _CFG["ssh_pw"]
FRIDA_PORT = int(_CFG["frida_port"])
FRIDA_HOST = "127.0.0.1:%d" % FRIDA_PORT
SSH_PORT = int(_CFG["ssh_local_port"])                       # local -> device SSH
SSH_DEVICE_PORT = int(_CFG["ssh_device_port"])               # dropbear 44 (rootless) / OpenSSH 22 (rootful)
DEV_FRIDA_BIN = _CFG["jb_prefix"].rstrip("/") + "/usr/sbin/frida-server"
GADGET_PORT = int(_CFG["gadget_port"])                       # frida-gadget listen port (anti-frida bypass)
GADGET_HOST = "127.0.0.1:%d" % GADGET_PORT


def _sh(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _iproxy(local: int, remote: int):
    # idempotent: leave existing tunnels alone, only (re)start if missing
    out = _sh(["pgrep", "-f", f"iproxy {local} {remote}"]).stdout.strip()
    if not out:
        subprocess.Popen([IPROXY, str(local), str(remote)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.5)


class IOSDevice:
    def __init__(self):
        self._dev: Optional[frida.core.Device] = None
        self._sb = None          # SpringBoard session
        self._sb_api = None
        self._fg = None          # (pid, session, api) cache for foreground app
        self._tel_api = None      # telephony/TTS session (tel.js in SpringBoard)
        self._keepalive: Optional[subprocess.Popen] = None

    # ------------------------------------------------------------------ frida
    def _try_connect(self) -> Optional[frida.core.Device]:
        try:
            d = frida.get_device_manager().add_remote_device(FRIDA_HOST)
            d.enumerate_processes()
            return d
        except Exception:
            return None

    def ensure_frida(self):
        """Connect to frida-server, starting+holding it alive if needed."""
        _iproxy(FRIDA_PORT, FRIDA_PORT)
        d = self._try_connect()
        if d:
            self._dev = d
            return
        # start frida-server held alive in a foreground SSH session
        _iproxy(SSH_PORT, SSH_DEVICE_PORT)
        exp = os.path.join(ROOT, "tools", "_keepfrida.exp")
        with open(exp, "w") as f:
            f.write(
                "set timeout -1\n"
                f"spawn ssh -p {SSH_PORT} -o StrictHostKeyChecking=no "
                "-o UserKnownHostsFile=/dev/null -o PreferredAuthentications=password "
                "-o PubkeyAuthentication=no -o NumberOfPasswordPrompts=1 "
                "-o ServerAliveInterval=30 root@127.0.0.1 "
                f"{{pkill -9 frida-server; sleep 1; exec {DEV_FRIDA_BIN} -l 0.0.0.0:{FRIDA_PORT}}}\n"
                "expect { -re {[Pp]assword:} {send \"" + SSH_PW + "\\r\"; exp_continue} eof {} }\n"
            )
        self._keepalive = subprocess.Popen(["expect", "-f", exp],
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(15):
            time.sleep(1)
            d = self._try_connect()
            if d:
                self._dev = d
                return
        raise RuntimeError("could not start/connect frida-server")

    @property
    def dev(self) -> frida.core.Device:
        if self._dev is None:
            self.ensure_frida()
        return self._dev  # type: ignore

    def _springboard(self):
        if self._sb_api is None:
            s = self.dev.attach("SpringBoard")
            sc = s.create_script(open(AGENT_JS).read())
            sc.load()
            self._sb, self._sb_api = s, sc.exports_sync
            self._sb_api.keep_awake()
        return self._sb_api

    def _foreground(self):
        """Attach to (and cache) the frontmost app; returns (identifier, api) or (id, None) if anti-frida."""
        app = self.dev.get_frontmost_application()
        ident = app.identifier if app else None
        if self._fg and self._fg[0] == (app.pid if app else None):
            return ident, self._fg[2]
        if not app:
            return None, None
        try:
            s = self.dev.attach(app.pid)
            sc = s.create_script(open(AGENT_JS).read())
            sc.load()
            self._fg = (app.pid, s, sc.exports_sync)
            return ident, sc.exports_sync
        except Exception:
            # frida-hardened app (anti-attach) -> try the gadget bypass if active
            gapi = self._gadget_api()
            if gapi is not None:
                self._fg = (app.pid, None, gapi)
                return ident, gapi
            self._fg = None
            return ident, None

    def _gadget_api(self):
        """Attach via a frida-gadget listening on GADGET_PORT (anti-frida bypass).

        Returns the agent exports, or None if no gadget is active. The gadget is
        injected at launch by ElleKit/TweakInject (scoped to the target bundle),
        so there is no ptrace attach for the app's anti-frida checks to detect.
        Activating it is a separate, user-authorized step (see README / the
        scripts/install_gadget_bypass.sh helper)."""
        try:
            _iproxy(GADGET_PORT, GADGET_PORT)
            gd = frida.get_device_manager().add_remote_device(GADGET_HOST)
            procs = gd.enumerate_processes()
            if not procs:
                return None
            s = gd.attach(procs[0].pid)
            sc = s.create_script(open(AGENT_JS).read())
            sc.load()
            return sc.exports_sync
        except Exception:
            return None

    # ------------------------------------------------------------------ go-ios
    def screenshot(self, path: str = "/tmp/mimic_screen.png") -> str:
        _sh([GOIOS, "screenshot", f"--output={path}"], timeout=25)
        return path

    def launch(self, bundle: str) -> bool:
        r = _sh([GOIOS, "launch", bundle], timeout=20)
        return "Process launched" in (r.stdout + r.stderr) or "started successfully" in (r.stdout + r.stderr)

    def apps(self) -> list[dict]:
        # frida LSApplicationWorkspace gives bundle+name; fall back to go-ios
        try:
            return self._springboard().apps()
        except Exception:
            r = _sh([GOIOS, "apps"], timeout=20)
            return [{"bundle": b, "name": ""} for b in
                    set(__import__("re").findall(r"[a-zA-Z0-9.\-]+\.[a-zA-Z0-9.\-]+", r.stdout))]

    def search_apps(self, q: str) -> list[dict]:
        q = q.lower()
        return [a for a in self.apps()
                if q in a["bundle"].lower() or q in (a.get("name") or "").lower()]

    def current_app(self) -> Optional[str]:
        app = self.dev.get_frontmost_application()
        return app.identifier if app else None

    def close_app(self, bundle: Optional[str] = None) -> dict:
        """Force-quit an app (frontmost if bundle is None). Removes it from the switcher."""
        try:
            if bundle is None:
                app = self.dev.get_frontmost_application()
                if not app:
                    return {"ok": 0, "error": "no frontmost app"}
                self._fg = None
                self.dev.kill(app.pid)
                return {"ok": 1, "killed": app.identifier}
            for a in self.dev.enumerate_applications():
                if a.identifier == bundle and a.pid:
                    if self._fg and self._fg[0] == a.pid:
                        self._fg = None
                    self.dev.kill(a.pid)
                    return {"ok": 1, "killed": bundle}
            return {"ok": 0, "error": "not running: " + bundle}
        except Exception as e:
            return {"ok": 0, "error": str(e)}

    # ------------------------------------------------------------ system (frida)
    def home(self):
        return self._springboard().home()

    def wake(self):
        return self._springboard().wake()

    def unlock(self):
        return self._springboard().unlock()

    def wake_unlock(self):
        """Wake + reliably dismiss the lock screen (no-passcode). Lands on home if
        locked; no-op if already unlocked. See agent.js wakeUnlock for why a plain
        unlock isn't enough on iOS 16.7."""
        return self._springboard().wake_unlock()

    def keep_awake(self):
        return self._springboard().keep_awake()

    # ------------------------------------------------------------ SSLKillSwitch3
    def _ssl_paths(self):
        """Candidate prefs paths for SSLKillSwitch3 (jb-prefixed first)."""
        jb = _CFG["jb_prefix"].rstrip("/")
        leaf = "/var/mobile/Library/Preferences/com.nablac0d3.SSLKillSwitchSettings.plist"
        return [jb + leaf, leaf]

    def ssl_status(self) -> dict:
        r = self._springboard().ssl_get(self._ssl_paths())
        return {"bypass": bool(r.get("bypass")), "found": bool(r.get("found")),
                "path": r.get("path")}

    def ssl_set(self, enable: bool, relaunch: Optional[str] = None) -> dict:
        """Toggle SSL Kill Switch 3 (disable cert validation = bypass pinning). The
        tweak reads its pref at each app's launch, so the change applies to apps
        started AFTER this; pass `relaunch` to kill+launch one app to apply now."""
        r = self._springboard().ssl_set(self._ssl_paths(), bool(enable))
        out = {"ok": bool(r.get("ok")), "bypass": bool(r.get("bypass")), "path": r.get("path")}
        if relaunch:
            try:
                self.close_app(relaunch)
                time.sleep(0.6)
                self.launch(relaunch)
                out["relaunched"] = relaunch
            except Exception as e:
                out["relaunch_error"] = str(e)
        return out

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300):
        return self._springboard().swipe(x1, y1, x2, y2, ms)

    def scroll(self, direction: str = "up", amount: int = 1):
        w, h = 750, 1334
        cx = w // 2
        moves = {"up": (cx, int(h * 0.7), cx, int(h * 0.3)),
                 "down": (cx, int(h * 0.3), cx, int(h * 0.7)),
                 "left": (int(w * 0.8), h // 2, int(w * 0.2), h // 2),
                 "right": (int(w * 0.2), h // 2, int(w * 0.8), h // 2)}
        x1, y1, x2, y2 = moves.get(direction, moves["up"])
        for _ in range(max(1, amount)):
            self.swipe(x1, y1, x2, y2, 300)
            time.sleep(0.25)
        return True

    # ----------------------------------------------------- in-app (frida foreground)
    def look(self, actionable_only: bool = True) -> dict:
        """Token-efficient screen read: app id + compact actionable element list."""
        ident, api = self._foreground()
        if api is None:
            # anti-frida or home screen: fall back to SpringBoard elements if home
            if ident is None:
                api = self._springboard()
            else:
                return {"app": ident, "elements": [], "note": "anti-frida app: in-app a11y unavailable (use gadget bypass)"}
        els = api.ui()
        if not isinstance(els, list):
            return {"app": ident, "elements": [], "error": els}
        out, seen = [], set()
        for e in els:
            lbl = (e.get("label") or "").strip()
            role = e.get("role")
            if not lbl:
                continue
            # actionable = real controls OR short labels (custom buttons / table cells /
            # links often dump as role 'txt' but are tappable); skip long paragraphs.
            tappable = role in ("btn", "ctl", "field", "switch", "slider") or (role == "txt" and len(lbl) <= 40)
            if actionable_only and not tappable:
                continue
            key = (lbl, (e.get("cx") or 0) // 8, (e.get("cy") or 0) // 8)
            if key in seen:
                continue
            seen.add(key)
            out.append({"role": role, "label": lbl, "x": e.get("cx"), "y": e.get("cy")})
        return {"app": ident, "elements": out[:70]}

    def tap_label(self, label: str, index: int = 0) -> dict:
        """'Tap' an element by label via its accessibility action (sendActions)."""
        ident, api = self._foreground()
        if api is None:
            return {"ok": 0, "error": "no frida access to foreground app", "app": ident}
        return api.activate(label, index)

    def type_text(self, text: str, field_label: str = "") -> dict:
        ident, api = self._foreground()
        if api is None:
            return {"ok": 0, "error": "no frida access to foreground app", "app": ident}
        return api.set_text(field_label, text)

    def record_video(self, seconds: float = 5.0, fps: int = 10,
                     out: str = "/tmp/mimic_video.mp4", quality: float = 0.5) -> dict:
        """Record the live screen (any app) to an mp4.

        Captures the real composited display on-device via CARenderServer (from
        SpringBoard), pulls the JPEG frames over frida, and assembles them to
        H.264 with tools/mkvideo. No external deps / no developer image needed.
        """
        api = self._springboard()
        devdir = "/var/jb/tmp/mimicrec"
        r = api.rec_run(devdir, int(fps), float(seconds), float(quality))
        if not isinstance(r, dict) or not r.get("ok"):
            return {"ok": 0, "error": "rec_run failed", "detail": r}
        n = int(r.get("frames", 0))
        if n <= 0:
            return {"ok": 0, "error": "no frames captured"}
        # pull frames over frida (scp/dropbear is flaky; frida is reliable)
        local = "/tmp/mimic_frames"
        _sh(["rm", "-rf", local]); os.makedirs(local, exist_ok=True)
        got = 0
        for i in range(n):
            name = "f%05d.jpg" % i
            b64 = api.read_file(devdir + "/" + name)
            if not b64:
                continue
            with open(os.path.join(local, name), "wb") as f:
                f.write(base64.b64decode(b64))
            got += 1
        if got == 0:
            return {"ok": 0, "error": "frame pull failed"}
        asm_fps = max(1, round(r.get("fps") or fps))
        mk = os.path.join(ROOT, "tools", "mkvideo")
        res = _sh([mk, local, str(asm_fps), out], timeout=120)
        ok = os.path.exists(out) and os.path.getsize(out) > 0
        return {"ok": 1 if ok else 0, "out": out if ok else None,
                "frames": got, "fps": round(r.get("fps") or fps, 1),
                "seconds": round(r.get("secs") or seconds, 2),
                "size": os.path.getsize(out) if ok else 0,
                "assemble": (res.stdout + res.stderr).strip()[-160:]}

    # ----------------------------------------------------- telephony + TTS (frida)
    def _tel(self):
        if self._tel_api is None:
            s = self.dev.attach("SpringBoard")
            sc = s.create_script(open(TEL_JS).read())
            sc.load()
            self._tel_api = sc.exports_sync
        return self._tel_api

    def speak(self, text: str, lang: str = "th-TH", rate: float = 0.48) -> dict:
        """Speak text aloud on the device (no call). Useful to test TTS."""
        api = self._tel()
        r = api.speak(text, lang, rate, 1.0)
        return r if isinstance(r, dict) else {"ok": 1}

    def _incall_agent(self):
        """Attach the control agent to the in-call UI process, used to speak into
        the call's telephony uplink during an active call. Returns (session, api)."""
        names = ["InCallService"]
        try:
            for p in self.dev.enumerate_processes():
                if any(k in p.name for k in ("InCall", "TelephonyUI")) and p.name not in names:
                    names.append(p.name)
        except Exception:
            pass
        last = None
        for nm in names:
            try:
                s = self.dev.attach(nm)
                sc = s.create_script(open(AGENT_JS).read())
                sc.load()
                return s, sc.exports_sync
            except Exception as e:
                last = e
        raise RuntimeError("no in-call process to attach (%s)" % last)

    def call_and_speak(self, number: str, text: str, lang: str = "th-TH",
                       rate: float = 0.48, answer_timeout: float = 40.0,
                       hang_after: bool = False, pitch: float = 1.0) -> dict:
        """Place a call; once the callee ANSWERS, speak `text` straight into the
        call's telephony UPLINK via AVSpeechSynthesizer.mixToTelephonyUplink (the
        official iOS API) so the callee hears it. Fully on-device — no
        speakerphone, no acoustic relay, no baseband injection. (Cellular call
        voice is baseband-sealed, so buffer injection is impossible; this routes
        the synthesized speech into the uplink at the system level instead.)"""
        api = self._tel()
        d = api.dial(number)
        if isinstance(d, dict) and d.get("err"):
            return {"ok": 0, "stage": "dial", "error": d["err"]}
        # wait for the callee to answer (or give up / disconnect)
        t0 = time.time(); state = "none"; saw_call = False
        while time.time() - t0 < answer_timeout:
            state = api.call_state()
            if state in ("dialing", "active", "incoming", "connected"):
                saw_call = True
            if state == "connected":
                break
            if saw_call and state == "none":
                return {"ok": 0, "stage": "wait", "result": "ended before answer"}
            time.sleep(0.5)
        if state != "connected":
            return {"ok": 0, "stage": "wait", "result": "no answer", "last_state": state}
        # answered -> speak into the uplink from the in-call process
        try:
            sess, ic = self._incall_agent()
        except Exception as e:
            return {"ok": 0, "stage": "attach", "error": str(e), "answered": True}
        r = ic.speak_uplink(text, lang, rate, pitch)
        if isinstance(r, dict) and r.get("err"):
            try: sess.detach()
            except Exception: pass
            return {"ok": 0, "stage": "speak", "error": r["err"], "answered": True}
        # wait for the speech to finish (poll isSpeaking, generous cap)
        time.sleep(0.6)
        spoke = time.time()
        while time.time() - spoke < 45:
            try:
                if not ic.uplink_speaking():
                    break
            except Exception:
                break
            time.sleep(0.3)
        result = {"ok": 1, "answered": True, "via": "mixToTelephonyUplink",
                  "voice": (r.get("voice") if isinstance(r, dict) else None),
                  "spoke_seconds": round(time.time() - spoke, 1)}
        if hang_after:
            try:
                result["hangup"] = api.hangup()
            except Exception:
                self._tel_api = None
                result["hangup"] = "call already ended"
        try: sess.detach()
        except Exception: pass
        return result

    def hangup(self) -> dict:
        return self._tel().hangup()

    def app_screenshot_b64(self) -> Optional[str]:
        ident, api = self._foreground()
        if api is None:
            return None
        o = api.shot()
        return o.get("png_b64") if isinstance(o, dict) else None

    def close(self):
        try:
            if self._fg:
                self._fg[1].detach()
        except Exception:
            pass


if __name__ == "__main__":
    import sys
    dev = IOSDevice()
    dev.ensure_frida()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "look"
    if cmd == "shot":
        print(dev.screenshot())
    elif cmd == "look":
        print(json.dumps(dev.look(), ensure_ascii=False, indent=2))
    elif cmd == "launch":
        print("launched:", dev.launch(sys.argv[2]))
    elif cmd == "tap":
        print(dev.tap_label(sys.argv[2]))
    elif cmd == "type":
        print(dev.type_text(sys.argv[2]))
    elif cmd == "apps":
        for a in dev.search_apps(sys.argv[2] if len(sys.argv) > 2 else ""):
            print(a["bundle"], "=", a.get("name"))
