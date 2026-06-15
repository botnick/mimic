#!/usr/bin/env bash
#
# install_gadget_bypass.sh — activate the per-app anti-frida bypass for ONE app.
#
# WHAT IT DOES (and why it needs your explicit consent):
#   Anti-frida apps (e.g. IDOL+ / com.xhxy.tala) reject a frida *attach*, so the
#   normal in-app accessibility path is unavailable for them. The bypass injects
#   frida-gadget at LAUNCH via ElleKit/TweakInject instead of attaching — there is
#   no ptrace attach for the app's anti-frida checks to catch. The gadget listens
#   on 127.0.0.1:27052 and Mimic connects to it like a normal frida session.
#
#   This installs a persistent dylib that auto-loads into the target app on every
#   launch. It is scoped STRICTLY to the one bundle id you pass — it does NOT
#   inject globally. Run it yourself (or explicitly authorize it); Mimic will not
#   install persistence on its own.
#
# USAGE:
#   scripts/install_gadget_bypass.sh com.xhxy.tala
#   scripts/install_gadget_bypass.sh --remove          # uninstall the bypass
#
# REQUIREMENTS: iproxy 44022->44 tunnel up, device SSH (root/alpine),
#   tools/gadget-arm64.dylib present (signed frida-gadget for arm64e).
#
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
GADGET="$HERE/tools/gadget-arm64.dylib"
SSH_PORT=44022
PW=alpine
DEVDIR=/var/jb/usr/lib/TweakInject
NAME=MimicGadget
PORT=27052

ssh_dev() {  # run a command on device
  expect <<EOF
set timeout 60
spawn ssh -p $SSH_PORT -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  -o PreferredAuthentications=password -o PubkeyAuthentication=no root@127.0.0.1 \
  "export PATH=/var/jb/usr/bin:/var/jb/bin:/var/jb/usr/sbin:/var/jb/sbin:\$PATH; $1"
expect { -re {[Pp]assword:} {send "$PW\r"; exp_continue} eof {} }
EOF
}
scp_dev() {  # scp \$1 (local) -> \$2 (device path)
  expect <<EOF
set timeout 180
spawn scp -P $SSH_PORT -O -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  -o PreferredAuthentications=password -o PubkeyAuthentication=no "$1" root@127.0.0.1:"$2"
expect { -re {[Pp]assword:} {send "$PW\r"; exp_continue} eof {} }
EOF
}

if [ "${1:-}" = "--remove" ]; then
  ssh_dev "rm -f $DEVDIR/$NAME.dylib $DEVDIR/$NAME.config $DEVDIR/$NAME.plist; echo removed"
  echo "Bypass uninstalled. Relaunch the target app for it to take effect."
  exit 0
fi

BUNDLE="${1:?usage: install_gadget_bypass.sh <bundle-id> | --remove}"
[ -f "$GADGET" ] || { echo "missing $GADGET"; exit 1; }

# 1) config: gadget listens, resumes app immediately (no hang on launch)
cat > /tmp/$NAME.config <<EOF
{ "interaction": { "type": "listen", "address": "127.0.0.1", "port": $PORT, "on_load": "resume" } }
EOF
# 2) filter plist scoped to EXACTLY this bundle (NOT global)
cat > /tmp/$NAME.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>Filter</key><dict>
  <key>Bundles</key><array><string>$BUNDLE</string></array>
</dict></dict></plist>
EOF

echo ">> pushing gadget + config + scoped filter to $DEVDIR ..."
scp_dev "$GADGET"        "$DEVDIR/$NAME.dylib"
scp_dev /tmp/$NAME.config "$DEVDIR/$NAME.config"
scp_dev /tmp/$NAME.plist  "$DEVDIR/$NAME.plist"
ssh_dev "cd $DEVDIR && ldid -S $NAME.dylib && echo SIGNED && ls -la $NAME.*"

cat <<EOF

Done. The bypass is scoped to: $BUNDLE
Next:
  1) iproxy $PORT $PORT          # tunnel the gadget port (Mimic's device.py does this automatically)
  2) relaunch the app:  tools/goios/ios launch $BUNDLE
  3) Mimic's mimic_look / mimic_tap now work inside it (device.py auto-detects the gadget).

If the app still detects the gadget (heavy commercial RASP scans loaded images for
frida/gum), the next step is a no-frida bespoke introspection dylib — see README.
Remove anytime with:  scripts/install_gadget_bypass.sh --remove
EOF
