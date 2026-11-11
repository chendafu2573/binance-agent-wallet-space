#!/bin/zsh
# 双击：立刻停桥（急停 + 取消开机自启）。要恢复就再双击 install.command。
BRIDGE_DIR="${0:A:h}"
ROOT="${BRIDGE_DIR:h}"
PLIST="$HOME/Library/LaunchAgents/com.baw.bridge.plist"
touch "$ROOT/bridge/STOP"
launchctl unload "$PLIST" 2>/dev/null
echo ""
echo "桥已停。看板里再点卖出只会登记，不会真卖。"
echo ""
read -k1 "?按任意键关掉这个窗口"
