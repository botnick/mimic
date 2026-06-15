#!/usr/bin/env bash
#
# install_frida_daemon.sh — make frida-server PERSISTENT via launchd.
#
# By default Mimic starts frida-server on demand and holds it alive over an SSH
# session (fragile: dies if that session/iproxy drops, or after a userspace
# reboot). This installs a proper LaunchDaemon so frida-server starts at boot
# and is restarted automatically if it dies — no held session needed.
#
# Run it yourself (or explicitly authorize it): installing a persistent daemon
# is a deliberate, user-authorized change — Mimic will not do it on its own.
#
# USAGE:
#   scripts/install_frida_daemon.sh            # install + load now
#   scripts/install_frida_daemon.sh --remove   # unload + remove
#
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
SSH_PORT=44022
PW=alpine
PLIST=/var/jb/Library/LaunchDaemons/re.frida.server.plist

ssh_dev(){ expect <<EOF
set timeout 40
spawn ssh -p $SSH_PORT -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  -o PreferredAuthentications=password -o PubkeyAuthentication=no root@127.0.0.1 \
  "export PATH=/var/jb/usr/bin:/var/jb/usr/sbin:/var/jb/bin:/var/jb/sbin:\$PATH; $1"
expect { -re {[Pp]assword:} {send "$PW\r"; exp_continue} eof {} }
EOF
}

if [ "${1:-}" = "--remove" ]; then
  ssh_dev "launchctl bootout system $PLIST 2>/dev/null; rm -f $PLIST; echo removed"
  echo "frida daemon removed. (on-demand start still works)"
  exit 0
fi

# Write the LaunchDaemon plist on-device, then bootstrap it.
read -r -d '' PLIST_XML <<'XML' || true
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>re.frida.server</string>
  <key>ProgramArguments</key>
  <array>
    <string>/var/jb/usr/sbin/frida-server</string>
    <string>-l</string>
    <string>0.0.0.0:27042</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/var/jb/var/log/frida.log</string>
  <key>StandardErrorPath</key><string>/var/jb/var/log/frida.log</string>
</dict>
</plist>
XML

B64=$(printf '%s' "$PLIST_XML" | base64)
ssh_dev "mkdir -p /var/jb/Library/LaunchDaemons; echo $B64 | base64 -d > $PLIST; chown root:wheel $PLIST; chmod 644 $PLIST; launchctl bootout system $PLIST 2>/dev/null; launchctl bootstrap system $PLIST && echo BOOTSTRAPPED; launchctl kickstart -k system/re.frida.server 2>/dev/null; sleep 1; (launchctl print system/re.frida.server 2>/dev/null | grep -iE 'state|pid' | head -3) || echo loaded"
echo
echo "frida-server is now a persistent daemon (re.frida.server) — starts at boot, auto-restarts."
echo "Remove with:  scripts/install_frida_daemon.sh --remove"
