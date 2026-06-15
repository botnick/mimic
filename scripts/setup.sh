#!/usr/bin/env bash
#
# setup.sh — universal one-command installer for Mimic.
#
# Run it on a Mac with a *jailbroken* iPhone plugged in over USB. It detects the
# device, installs everything Mimic needs, writes a per-device config, and
# registers the MCP — so moving Mimic to a new jailbroken device is one command.
#
# What it does (idempotent — safe to re-run):
#   1. Mac prereqs:  python3 + frida (pip), iproxy + ldid (brew), bundled go-ios
#   2. detect device (UDID / iOS / model) and mount the DeveloperDiskImage
#   3. detect SSH (dropbear :44 rootless / OpenSSH :22 rootful) + jailbreak prefix
#   4. install frida-server (bundled universal arm64+arm64e, ldid-signed) and a
#      LaunchDaemon so it auto-starts at boot (KeepAlive) — no held session
#   5. write mimic.config.json (ports / prefix / ssh) so device.py adapts
#   6. start the iproxy tunnel and verify Frida connects
#   7. register the MCP:  claude mcp add mimic -- python3 .../mimic/server.py
#
# Usage:
#   scripts/setup.sh [options]
#     --ssh-pass PW       device root password (default: alpine)
#     --ssh-port N        device-side SSH port (default: autodetect 44,22,2222)
#     --jb-prefix PATH    jailbreak prefix override (default: autodetect /var/jb or /)
#     --frida-version V   download frida-server V instead of the bundled binary
#     --force-frida       reinstall frida-server even if it already answers
#     --no-daemon         don't install the LaunchDaemon (use held-session start)
#     --no-mcp            don't run `claude mcp add`
#     --skip-frida        skip the frida-server step entirely (already installed)
#
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
GOIOS="$ROOT/tools/goios/ios"
FRIDA_LOCAL="$ROOT/tools/frida-server-arm64e"      # bundled universal binary
ENTS_LOCAL="$ROOT/tools/frida-entitlements.plist"
BUNDLED_FRIDA_VER="16.1.4"                          # version of the bundled binary (client must match)
MAC_FRIDA_VER=""

FRIDA_PORT=27042
SSH_LOCAL=44022
DEV_SSH_PORT=""
SSH_PASS="alpine"
JB_PREFIX=""
FRIDA_VERSION=""
GADGET_PORT=27052
DO_MCP=1; DO_DAEMON=1; FORCE_FRIDA=0; SKIP_FRIDA=0

while [ $# -gt 0 ]; do case "$1" in
  --ssh-pass) SSH_PASS="$2"; shift 2;;
  --ssh-port) DEV_SSH_PORT="$2"; shift 2;;
  --jb-prefix) JB_PREFIX="$2"; shift 2;;
  --frida-version) FRIDA_VERSION="$2"; shift 2;;
  --force-frida) FORCE_FRIDA=1; shift;;
  --no-daemon) DO_DAEMON=0; shift;;
  --no-mcp) DO_MCP=0; shift;;
  --skip-frida) SKIP_FRIDA=1; shift;;
  -h|--help) sed -n '2,33p' "$0"; exit 0;;
  *) echo "unknown option: $1" >&2; exit 1;;
esac; done

c_i='\033[1;36m'; c_w='\033[1;33m'; c_e='\033[1;31m'; c_0='\033[0m'
log(){  printf "${c_i}[mimic]${c_0} %s\n" "$*"; }
warn(){ printf "${c_w}[warn]${c_0}  %s\n" "$*"; }
die(){  printf "${c_e}[fail]${c_0}  %s\n" "$*" >&2; exit 1; }

DEV_PATH='/var/jb/usr/bin:/var/jb/usr/sbin:/var/jb/bin:/var/jb/sbin:/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin'

# ---------------------------------------------------------------- Mac prereqs
find_bin(){ local n="$1"; shift; local c; for c in "$@"; do [ -x "${c/#\~/$HOME}" ] && { echo "${c/#\~/$HOME}"; return; }; done; command -v "$n" 2>/dev/null || true; }

mac_prereqs(){
  command -v python3 >/dev/null || die "python3 is required"
  if python3 -c 'import frida' 2>/dev/null; then
    MAC_FRIDA_VER="$(python3 -c 'import frida;print(frida.__version__)' 2>/dev/null)"
  else
    log "installing frida==$BUNDLED_FRIDA_VER (python client, pinned to the bundled server)…"
    python3 -m pip install --user -q "frida==$BUNDLED_FRIDA_VER" || die "pip install frida failed"
    MAC_FRIDA_VER="$BUNDLED_FRIDA_VER"
  fi
  IPROXY="$(find_bin iproxy ~/homebrew/bin/iproxy /opt/homebrew/bin/iproxy /usr/local/bin/iproxy)"
  if [ -z "$IPROXY" ]; then
    command -v brew >/dev/null && { log "installing libimobiledevice (iproxy)…"; brew install -q libimobiledevice; IPROXY="$(command -v iproxy)"; }
  fi
  [ -n "$IPROXY" ] || die "iproxy not found (brew install libimobiledevice)"
  LDID="$(find_bin ldid ~/homebrew/bin/ldid /opt/homebrew/bin/ldid /usr/local/bin/ldid)"
  [ -n "$LDID" ] || { command -v brew >/dev/null && { log "installing ldid…"; brew install -q ldid; LDID="$(command -v ldid)"; }; }
  [ -x "$GOIOS" ] || die "bundled go-ios missing at $GOIOS"
  log "mac prereqs ok  (frida=$MAC_FRIDA_VER  iproxy=$IPROXY  ldid=${LDID:-none})"
}

ensure_iproxy(){ pgrep -f "iproxy $1 $2" >/dev/null 2>&1 || { "$IPROXY" "$1" "$2" >/dev/null 2>&1 & sleep 1; }; }

# ---------------------------------------------------------------- device (go-ios)
jget(){ python3 -c "import sys,json
try: print(json.load(sys.stdin).get('$1',''))
except Exception: print('')"; }

detect_device(){
  UDID="$("$GOIOS" list 2>/dev/null | python3 -c 'import sys,json
try: print(json.load(sys.stdin)["deviceList"][0])
except Exception: print("")' )"
  [ -n "$UDID" ] || die "no device found — plug the iPhone in directly (not a hub) and trust it"
  local info; info="$("$GOIOS" info 2>/dev/null || true)"
  IOSVER="$(printf '%s' "$info" | jget ProductVersion)"
  PTYPE="$(printf '%s' "$info" | jget ProductType)"
  log "device: ${PTYPE:-?}  iOS ${IOSVER:-?}  ($UDID)"
}

mount_ddi(){
  log "mounting DeveloperDiskImage (for launch/screenshot)…"
  "$GOIOS" image auto --basedir="$ROOT/tools/goios/ddi" >/dev/null 2>&1 \
    && log "  DDI mounted" || warn "  DDI auto-mount failed (frida path still works)"
}

# ---------------------------------------------------------------- SSH helpers
# raw: run a flat command on the device over password SSH, echo the full transcript
ssh_raw(){ # cmd [timeout]
  local cmd="$1" to="${2:-40}"
  expect -c "
    set timeout $to
    log_user 1
    spawn ssh -p $SSH_LOCAL -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o PreferredAuthentications=password -o PubkeyAuthentication=no -o NumberOfPasswordPrompts=2 \
      root@127.0.0.1 {PATH=$DEV_PATH; $cmd}
    expect { -re {[Pp]assword:} {send \"$SSH_PASS\r\"; exp_continue} eof {} }
  " 2>/dev/null
}
# run a command, succeed only if its output contains MARKER (retries transient dropbear denials)
ssh_ok(){ # cmd marker [timeout]
  local cmd="$1" marker="$2" to="${3:-20}" i
  for i in 1 2 3; do
    ssh_raw "$cmd" "$to" | grep -q "$marker" && return 0
    sleep 1
  done
  return 1
}
# copy a local file to the device (scp -O legacy), exit with scp's real status; retries
scp_to(){ # local remote
  local lf="$1" rf="$2" try
  for try in 1 2 3; do
    expect -c "
      set timeout 300
      spawn scp -P $SSH_LOCAL -O -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o PreferredAuthentications=password -o PubkeyAuthentication=no -o NumberOfPasswordPrompts=2 \
        {$lf} root@127.0.0.1:$rf
      expect { -re {[Pp]assword:} {send \"$SSH_PASS\r\"; exp_continue} eof {} }
      catch wait result
      exit [lindex \$result 3]
    " >/dev/null 2>&1 && return 0
    sleep 2
  done
  return 1
}

detect_ssh(){
  local cand=("44" "22" "2222"); [ -n "$DEV_SSH_PORT" ] && cand=("$DEV_SSH_PORT")
  for p in "${cand[@]}"; do
    ensure_iproxy "$SSH_LOCAL" "$p"
    if ssh_ok 'echo MIMIC_OK' MIMIC_OK 12; then
      DEV_SSH_PORT="$p"; log "ssh ok on device port $p"; return 0
    fi
  done
  die "could not SSH to the device (tried ports ${cand[*]}; pass --ssh-port / --ssh-pass). On palera1n make sure dropbear/SSH is running."
}

detect_prefix(){
  [ -n "$JB_PREFIX" ] && { log "jb prefix (override): $JB_PREFIX"; return; }
  local out; out="$(ssh_raw 'test -d /var/jb && echo PFX=/var/jb || echo PFX=/' 12)"
  JB_PREFIX="$(printf '%s' "$out" | grep -o 'PFX=[^[:space:]]*' | head -1 | cut -d= -f2)"
  [ -z "$JB_PREFIX" ] && JB_PREFIX=/var/jb
  log "jailbreak prefix: $JB_PREFIX"
}

# ---------------------------------------------------------------- frida-server
frida_answers(){ python3 - "$FRIDA_PORT" <<'PY' 2>/dev/null
import sys, frida
try:
    d = frida.get_device_manager().add_remote_device("127.0.0.1:%s" % sys.argv[1])
    d.enumerate_processes(); print("OK")
except Exception: print("NO")
PY
}

fetch_frida(){ # downloads frida-server matching --frida-version into a temp file, echoes path
  local ver="$1" arch="arm64e" tmp; tmp="$(mktemp)"
  local url="https://github.com/frida/frida/releases/download/${ver}/frida-server-${ver}-ios-${arch}.xz"
  log "downloading frida-server $ver ($arch)…"
  if curl -fsSL "$url" -o "$tmp.xz" && xz -d "$tmp.xz" 2>/dev/null; then echo "$tmp"; else rm -f "$tmp" "$tmp.xz"; return 1; fi
}

install_frida(){
  [ "$SKIP_FRIDA" = 1 ] && { log "skip frida-server install (--skip-frida)"; return; }
  ensure_iproxy "$FRIDA_PORT" "$FRIDA_PORT"
  if [ "$FORCE_FRIDA" = 0 ] && [ "$(frida_answers)" = "OK" ]; then
    log "frida-server already answering on :$FRIDA_PORT — skipping install"
    return
  fi
  # the server version MUST match the Mac frida client or Frida won't connect
  local want="${FRIDA_VERSION:-$MAC_FRIDA_VER}" bin
  if [ -z "$FRIDA_VERSION" ] && [ "$want" = "$BUNDLED_FRIDA_VER" ]; then
    bin="$FRIDA_LOCAL"; log "using bundled frida-server $BUNDLED_FRIDA_VER (matches client)"
  else
    log "Mac frida client is $MAC_FRIDA_VER — fetching matching frida-server…"
    bin="$(fetch_frida "$want")" || die "couldn't get frida-server $want to match the client; run 'pip install frida==$BUNDLED_FRIDA_VER' or pass --frida-version"
  fi
  [ -f "$bin" ] || die "no frida-server binary"
  # the bundled binary is already correctly signed (pulled from a working device);
  # only (re)sign a freshly DOWNLOADED binary — on the Mac, so the device needs no ldid.
  local signed; signed="$(mktemp)"; cp "$bin" "$signed"
  if [ "$bin" != "$FRIDA_LOCAL" ] && [ -n "$LDID" ]; then
    "$LDID" -S"$ENTS_LOCAL" "$signed" 2>/dev/null || warn "Mac ldid sign failed (will try as-is)"
  fi
  local dst="$JB_PREFIX/usr/sbin/frida-server"
  log "pushing signed frida-server → $dst …"
  scp_to "$signed" "/tmp/frida-server" || { rm -f "$signed"; die "scp of frida-server failed (dropbear busy? re-run setup.sh)"; }
  ssh_ok "mkdir -p $JB_PREFIX/usr/sbin; mv -f /tmp/frida-server $dst; chmod 755 $dst; echo INSTALLED" INSTALLED 60 \
    || { rm -f "$signed"; die "on-device frida-server install failed"; }
  rm -f "$signed"
  log "frida-server $want installed at $dst"
}

install_daemon(){
  [ "$DO_DAEMON" = 0 ] && { log "skip LaunchDaemon (--no-daemon; held-session start will be used)"; return; }
  local lddir plist
  if [ "$JB_PREFIX" = "/" ]; then lddir="/Library/LaunchDaemons"; else lddir="$JB_PREFIX/Library/LaunchDaemons"; fi
  plist="$lddir/re.frida.server.plist"
  local tmpl; tmpl="$(mktemp)"
  cat > "$tmpl" <<XML
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>re.frida.server</string>
  <key>ProgramArguments</key><array>
    <string>$JB_PREFIX/usr/sbin/frida-server</string><string>-l</string><string>0.0.0.0:$FRIDA_PORT</string>
  </array>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/frida.log</string>
  <key>StandardErrorPath</key><string>/tmp/frida.log</string>
</dict></plist>
XML
  log "installing LaunchDaemon (auto-start at boot)…"
  scp_to "$tmpl" "/tmp/re.frida.server.plist" || { warn "scp of daemon plist failed; skipping daemon (on-demand start still works)"; rm -f "$tmpl"; return; }
  rm -f "$tmpl"
  ssh_ok "mkdir -p $lddir; mv -f /tmp/re.frida.server.plist $plist; chown root:wheel $plist; chmod 644 $plist; launchctl bootout system $plist 2>/dev/null; pkill -9 frida-server 2>/dev/null; sleep 1; launchctl bootstrap system $plist 2>/dev/null; launchctl kickstart -k system/re.frida.server 2>/dev/null; echo DAEMON_OK" DAEMON_OK 45 \
    || warn "daemon bootstrap returned no marker (may still have installed; check /tmp/frida.log on device)"
  log "LaunchDaemon installed ($plist)"
}

# ---------------------------------------------------------------- config + MCP
write_config(){
  cat > "$ROOT/mimic.config.json" <<JSON
{
  "iproxy": "$IPROXY",
  "frida_port": $FRIDA_PORT,
  "ssh_local_port": $SSH_LOCAL,
  "ssh_device_port": ${DEV_SSH_PORT:-44},
  "ssh_pw": "$SSH_PASS",
  "jb_prefix": "$JB_PREFIX",
  "gadget_port": $GADGET_PORT
}
JSON
  log "wrote mimic.config.json"
}

verify_frida(){
  ensure_iproxy "$FRIDA_PORT" "$FRIDA_PORT"
  local i
  for i in $(seq 1 20); do
    [ "$(frida_answers)" = "OK" ] && { log "Frida connected on :$FRIDA_PORT ✓"; return 0; }
    sleep 1
  done
  warn "Frida not answering yet — the daemon may still be starting; device.py will also start it on demand."
}

register_mcp(){
  [ "$DO_MCP" = 0 ] && { log "skip MCP registration (--no-mcp)"; return; }
  local claude; claude="$(command -v claude || true)"
  [ -z "$claude" ] && for c in /Applications/cmux.app/Contents/Resources/bin/claude ~/.claude/local/claude; do [ -x "$c" ] && claude="$c" && break; done
  [ -z "$claude" ] && { warn "claude CLI not found — register manually: claude mcp add mimic -- $(command -v python3) $ROOT/mimic/server.py"; return; }
  "$claude" mcp remove mimic >/dev/null 2>&1 || true
  if "$claude" mcp add mimic -- "$(command -v python3)" "$ROOT/mimic/server.py" >/dev/null 2>&1; then
    log "registered MCP 'mimic' ✓"
  else
    warn "MCP registration failed — run: claude mcp add mimic -- $(command -v python3) $ROOT/mimic/server.py"
  fi
}

smoke(){
  log "smoke test (frida look)…"
  python3 - <<PY 2>/dev/null || warn "smoke test inconclusive (device.py will retry on use)"
import sys; sys.path.insert(0, "$ROOT")
from mimic.ios.device import IOSDevice
d = IOSDevice(); d.ensure_frida()
print("[mimic]   frida procs:", len(d.dev.enumerate_processes()))
PY
}

# ----------------------------------------------------------------------- main
log "Mimic universal setup — repo: $ROOT"
mac_prereqs
detect_device
mount_ddi
detect_ssh
detect_prefix
install_frida
install_daemon
write_config
verify_frida
register_mcp
smoke
echo
log "done ✅  —  use it via the MCP tools (mimic_look, mimic_tap, mimic_call, …)"
log "re-run anytime; it's idempotent. Config: $ROOT/mimic.config.json"
