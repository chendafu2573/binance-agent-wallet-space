#!/bin/zsh
# 双击一次：把卖出桥注册成开机自启的常驻服务，并立刻启动。
# 目录约定：本文件位于 <工作区根>/bridge/ 下，工作区根里要有 .venv、chain/kit.py、bin/bawp。
BRIDGE_DIR="${0:A:h}"
ROOT="${BRIDGE_DIR:h}"
PLIST="$HOME/Library/LaunchAgents/com.baw.bridge.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/bridge/outbox" "$ROOT/bridge/results"
rm -f "$ROOT/bridge/STOP"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.baw.bridge</string>
  <key>ProgramArguments</key>
  <array>
    <string>$ROOT/.venv/bin/python</string>
    <string>$ROOT/bridge/bridge.py</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$ROOT/bridge/launchd.log</string>
  <key>StandardErrorPath</key><string>$ROOT/bridge/launchd.log</string>
</dict>
</plist>
EOF
launchctl unload "$PLIST" 2>/dev/null
launchctl load "$PLIST"
echo ""
echo "桥已启动，并设为开机自启。"
echo "要停桥：双击同文件夹里的 stop.command。"
echo ""
read -k1 "?按任意键关掉这个窗口"
