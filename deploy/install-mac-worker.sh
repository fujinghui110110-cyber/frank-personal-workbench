#!/bin/zsh
set -euo pipefail
umask 077

project_root="$(cd "$(dirname "$0")/.." && pwd)"
env_file="$project_root/.env"
python_bin="$project_root/.venv/bin/python"
node_bin="$(command -v node || true)"
syncthing_bin="$project_root/.tools/syncthing/syncthing"
template="$project_root/deploy/com.finance-workbench.worker.plist"
sync_template="$project_root/deploy/com.finance-workbench.sync.plist"
server_template="$project_root/deploy/com.finance-workbench.server.plist"
guide="$project_root/docs/安卓手机投递说明.md"
target="$HOME/Library/LaunchAgents/com.finance-workbench.worker.plist"
sync_target="$HOME/Library/LaunchAgents/com.finance-workbench.sync.plist"
server_target="$HOME/Library/LaunchAgents/com.finance-workbench.server.plist"
label="com.finance-workbench.worker"
sync_label="com.finance-workbench.sync"
server_label="com.finance-workbench.server"
legacy_sync_target="$HOME/Library/LaunchAgents/com.finance-workbench.wechat-sync.plist"
legacy_sync_label="com.finance-workbench.wechat-sync"
sync_inbox="${WORKBENCH_SYNC_INBOX:-$HOME/财务工作台投递箱}"
syncthing_home="$HOME/Library/Application Support/FinanceWorkbench/Syncthing"

if [[ ! -x "$python_bin" ]]; then
  echo "未找到本地转写环境：$python_bin" >&2
  exit 1
fi

if [[ ! -x "$node_bin" ]]; then
  echo "未找到 WorkBuddy 运行所需的 Node.js" >&2
  exit 1
fi
if [[ ! -x "$syncthing_bin" ]]; then
  echo "未找到安卓同步程序：$syncthing_bin" >&2
  exit 1
fi
if [[ ! -f "$env_file" ]]; then
  echo "未找到工作台配置：$env_file" >&2
  exit 1
fi

set -a
source "$env_file"
set +a
: "${WORKBENCH_WORKER_TOKEN:?缺少 WORKBENCH_WORKER_TOKEN}"

mkdir -p "$HOME/Library/LaunchAgents" "$sync_inbox/待处理" "$sync_inbox/已接收" "$syncthing_home"
launchctl bootout "gui/$(id -u)/$legacy_sync_label" >/dev/null 2>&1 || true
rm -f "$legacy_sync_target"
cp "$guide" "$sync_inbox/手机投递说明.md"
if [[ ! -f "$syncthing_home/config.xml" ]]; then
  "$syncthing_bin" generate --home "$syncthing_home" --no-port-probing >/dev/null
fi

cp "$sync_template" "$sync_target"
/usr/libexec/PlistBuddy -c "Set :ProgramArguments:0 $syncthing_bin" "$sync_target"
/usr/libexec/PlistBuddy -c "Set :ProgramArguments:2 --home=$syncthing_home" "$sync_target"
plutil -lint "$sync_target" >/dev/null
launchctl bootout "gui/$(id -u)/$sync_label" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$sync_target"
launchctl kickstart -k "gui/$(id -u)/$sync_label"

cp "$server_template" "$server_target"
plutil -lint "$server_target" >/dev/null
launchctl bootout "gui/$(id -u)/$server_label" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$server_target"
launchctl kickstart -k "gui/$(id -u)/$server_label"
sync_ready=0
folder_ids=""
for _ in {1..15}; do
  if folder_ids="$("$syncthing_bin" cli --home "$syncthing_home" config folders list 2>/dev/null)"; then
    sync_ready=1
    break
  fi
  sleep 1
done
if [[ "$sync_ready" -ne 1 ]]; then
  echo "安卓同步服务启动失败，请检查 /tmp/finance-workbench-sync-error.log" >&2
  exit 1
fi
if ! grep -Fxq "finance-workbench-inbox" <<<"$folder_ids"; then
  "$syncthing_bin" cli --home "$syncthing_home" config folders add \
    --id "finance-workbench-inbox" \
    --label "财务工作台投递箱" \
    --path "$sync_inbox" \
    --type "sendreceive" \
    --rescan-intervals 60 >/dev/null
fi

cp "$template" "$target"
chmod 600 "$target"
/usr/libexec/PlistBuddy -c "Set :ProgramArguments:0 $python_bin" "$target"
plutil -replace WorkingDirectory -string "$project_root" "$target"
plutil -replace EnvironmentVariables.WORKBENCH_BASE_URL -string "http://127.0.0.1:8000" "$target"
plutil -replace EnvironmentVariables.WORKBENCH_WORKER_TOKEN -string "$WORKBENCH_WORKER_TOKEN" "$target"
plutil -replace EnvironmentVariables.WORKBENCH_SYNC_INBOX -string "$sync_inbox" "$target"
plutil -replace EnvironmentVariables.PERSONAL_WECHAT_READER -string "${PERSONAL_WECHAT_READER:-direct}" "$target"
plutil -replace EnvironmentVariables.PERSONAL_WECHAT_DIRECT_ENABLED_AT -string "${PERSONAL_WECHAT_DIRECT_ENABLED_AT:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}" "$target"
plutil -replace EnvironmentVariables.PATH -string "$(dirname "$node_bin"):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" "$target"
plutil -lint "$target" >/dev/null

launchctl bootout "gui/$(id -u)/$label" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$target"
launchctl kickstart -k "gui/$(id -u)/$label"
echo "安卓同步和 Mac 自动处理已启动"
