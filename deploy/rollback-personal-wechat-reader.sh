#!/bin/zsh
set -euo pipefail
umask 077

backup_dir="${WORKBENCH_BACKUP_DIR:-}"
reader="ciphertalk"
enabled_at=""
launchagent_dir="${WORKBENCH_LAUNCHAGENT_DIR:-$HOME/Library/LaunchAgents}"
dry_run=0
label="com.finance-workbench.worker"

usage() {
  print -r -- "用法：zsh deploy/rollback-personal-wechat-reader.sh --backup-dir PATH [选项]"
  print -r -- "  --backup-dir PATH       含 launchagents/$label.plist 的备份目录"
  print -r -- "  --reader direct|ciphertalk  要写入 worker 的读取方式；默认 ciphertalk"
  print -r -- "  --enabled-at ISO_TIME       首次启用 direct 的起始时间；默认当前时间"
  print -r -- "  --launchagent-dir PATH  目标 LaunchAgent 目录"
  print -r -- "  --dry-run               只验证并显示计划，不修改 plist 或服务"
}

die() {
  print -u2 -r -- "错误：$*"
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "未找到命令：$1"
}

while (($#)); do
  case "$1" in
    --backup-dir)
      (($# >= 2)) || die "--backup-dir 缺少路径"
      backup_dir="$2"
      shift 2
      ;;
    --reader|--mode)
      (($# >= 2)) || die "$1 缺少读取方式"
      reader="$2"
      shift 2
      ;;
    --launchagent-dir)
      (($# >= 2)) || die "--launchagent-dir 缺少路径"
      launchagent_dir="$2"
      shift 2
      ;;
    --enabled-at)
      (($# >= 2)) || die "--enabled-at 缺少时间"
      enabled_at="$2"
      shift 2
      ;;
    --dry-run)
      dry_run=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      die "未知参数：$1"
      ;;
  esac
done

[[ -n "$backup_dir" ]] || die "必须显式提供 --backup-dir，避免误用错误备份"
case "$reader" in
  direct|ciphertalk) ;;
  *) die "读取方式只能是 direct 或 ciphertalk：$reader" ;;
esac

backup_worker="$backup_dir/launchagents/$label.plist"
target="$launchagent_dir/$label.plist"
[[ -f "$backup_worker" ]] || die "未找到 worker 备份副本：$backup_worker"
[[ ! -L "$backup_worker" ]] || die "worker 备份副本不能是符号链接：$backup_worker"
grep -Fq "<string>$label</string>" "$backup_worker" || die "备份 plist 的 Label 不是 $label"

if (( dry_run )); then
  print -r -- "dry-run：将使用 $backup_worker 作为 worker plist 副本"
  print -r -- "dry-run：将设置 PERSONAL_WECHAT_READER=$reader"
  print -r -- "dry-run：将只重启并验证 $label"
  print -r -- "dry-run：不会写入 $target，也不会调用 launchctl"
  exit 0
fi

require_command cp
require_command mkdir
require_command mv
require_command chmod
require_command grep
require_command id

plist_buddy="${PLIST_BUDDY:-/usr/libexec/PlistBuddy}"
plutil_bin="${PLUTIL_BIN:-plutil}"
launchctl_bin="${LAUNCHCTL_BIN:-launchctl}"
if [[ "$plist_buddy" == */* ]]; then
  [[ -x "$plist_buddy" ]] || die "未找到可执行 PlistBuddy：$plist_buddy"
else
  require_command "$plist_buddy"
fi
require_command "$plutil_bin"
require_command "$launchctl_bin"

mkdir -p "$launchagent_dir"
staged_target="$target.rollback.$$"
[[ ! -e "$staged_target" ]] || die "临时目标已存在，为避免覆盖而停止：$staged_target"
source_plist="$backup_worker"
[[ -f "$target" ]] && source_plist="$target"
cp -p "$source_plist" "$staged_target"

if ! "$plist_buddy" -c "Set :EnvironmentVariables:PERSONAL_WECHAT_READER $reader" "$staged_target" >/dev/null 2>&1; then
  "$plist_buddy" -c "Add :EnvironmentVariables:PERSONAL_WECHAT_READER string $reader" "$staged_target"
fi
if [[ "$reader" == "direct" ]]; then
  if [[ -n "$enabled_at" ]]; then
    direct_start="$enabled_at"
  elif direct_start="$($plist_buddy -c "Print :EnvironmentVariables:PERSONAL_WECHAT_DIRECT_ENABLED_AT" "$staged_target" 2>/dev/null)" && [[ -n "$direct_start" ]]; then
    :
  else
    direct_start="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  fi
  if ! "$plist_buddy" -c "Set :EnvironmentVariables:PERSONAL_WECHAT_DIRECT_ENABLED_AT $direct_start" "$staged_target" >/dev/null 2>&1; then
    "$plist_buddy" -c "Add :EnvironmentVariables:PERSONAL_WECHAT_DIRECT_ENABLED_AT string $direct_start" "$staged_target"
  fi
fi
"$plutil_bin" -lint "$staged_target" >/dev/null
if [[ -f "$target" ]]; then
  cp -p "$target" "$target.before-reader-switch-$(date +%Y%m%d-%H%M%S)"
fi
mv -f "$staged_target" "$target"
chmod 600 "$target"

domain="gui/$(id -u)"
service="$domain/$label"
"$launchctl_bin" bootout "$service" >/dev/null 2>&1 || true
"$launchctl_bin" bootstrap "$domain" "$target"
"$launchctl_bin" kickstart -k "$service"
state="$("$launchctl_bin" print "$service")"
if ! print -r -- "$state" | grep -Eq 'state = running|pid = [1-9][0-9]*'; then
  die "$service 已加载但未确认 running"
fi

print -r -- "个人微信读取方式已切换为 $reader；$service 正在运行。"
