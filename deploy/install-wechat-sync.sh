#!/bin/zsh
set -euo pipefail
umask 077

target="$HOME/Library/LaunchAgents/com.finance-workbench.wechat-sync.plist"
label="com.finance-workbench.wechat-sync"

launchctl bootout "gui/$(id -u)/$label" >/dev/null 2>&1 || true
rm -f "$target"

echo "已停用旧微信定时任务；微信检查由 com.finance-workbench.worker 统一调度。"
