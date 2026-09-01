#!/bin/zsh
set -euo pipefail
umask 077

project_root="$(cd "$(dirname "$0")/.." && pwd)"
backup_dir="${WORKBENCH_BACKUP_DIR:-}"
compose_file=""
dry_run=0
service_name="workbench"
worker_label="com.finance-workbench.worker"
launchagent_dir="${WORKBENCH_LAUNCHAGENT_DIR:-$HOME/Library/LaunchAgents}"

usage() {
  print -r -- "用法：zsh deploy/rollback-workbench-release.sh --backup-dir PATH [选项]"
  print -r -- "  --backup-dir PATH       显式指定完整版本备份目录"
  print -r -- "  --project-root PATH     Compose project directory"
  print -r -- "  --compose-file PATH     要恢复的当前 Compose 文件；默认 project_root/compose.yaml"
  print -r -- "  --launchagent-dir PATH  worker LaunchAgent 目录"
  print -r -- "  --dry-run               验证备份并显示计划，不修改 Docker"
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
    --project-root)
      (($# >= 2)) || die "--project-root 缺少路径"
      project_root="$2"
      shift 2
      ;;
    --compose-file)
      (($# >= 2)) || die "--compose-file 缺少路径"
      compose_file="$2"
      shift 2
      ;;
    --launchagent-dir)
      (($# >= 2)) || die "--launchagent-dir 缺少路径"
      launchagent_dir="$2"
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
[[ -d "$backup_dir" ]] || die "备份目录不存在：$backup_dir"
[[ ! -L "$backup_dir" ]] || die "备份目录不能是符号链接：$backup_dir"
if [[ -z "$compose_file" ]]; then
  compose_file="$project_root/compose.yaml"
fi
[[ -d "$project_root" ]] || die "项目根目录不存在：$project_root"
compose_parent="${compose_file:h}"
[[ -d "$compose_parent" ]] || die "Compose 文件父目录不存在：$compose_parent"
[[ ! -L "$compose_file" ]] || die "目标 Compose 文件不能是符号链接：$compose_file"
[[ ! -d "$compose_file" ]] || die "目标 Compose 路径不能是目录：$compose_file"

manifest="$backup_dir/SHA256SUMS"
image_id_file="$backup_dir/docker-image-id.txt"
image_tag_file="$backup_dir/docker-image-tag.txt"
database_file="$backup_dir/workbench.sqlite3"
objects_file=""
for candidate in "$backup_dir/objects.tgz" "$backup_dir/objects.tar.gz"; do
  if [[ -f "$candidate" ]]; then
    objects_file="$candidate"
    break
  fi
done
runtime_config=""
for candidate in \
  "$backup_dir/compose.yaml" \
  "$backup_dir/docker-compose.yml" \
  "$backup_dir/runtime/compose.yaml" \
  "$backup_dir/runtime/docker-compose.yml"; do
  if [[ -f "$candidate" ]]; then
    runtime_config="$candidate"
    break
  fi
done

[[ -f "$manifest" ]] || die "未找到 SHA256 manifest：$manifest"
[[ -f "$image_id_file" ]] || die "未找到 Docker image id：$image_id_file"
[[ -f "$image_tag_file" ]] || die "未找到 Docker image tag：$image_tag_file"
[[ -f "$database_file" ]] || die "未找到 formal SQLite 备份：$database_file"
[[ -n "$objects_file" ]] || die "未找到 objects tar 备份：$backup_dir"
[[ -n "$runtime_config" ]] || die "未找到备份的 runtime Compose 配置：$backup_dir"
[[ ! -L "$runtime_config" ]] || die "备份 runtime Compose 配置不能是符号链接：$runtime_config"
worker_plist="$backup_dir/launchagents/$worker_label.plist"
[[ -f "$worker_plist" ]] || die "未找到 worker LaunchAgent 备份：$worker_plist"
[[ ! -L "$worker_plist" ]] || die "worker LaunchAgent 备份不能是符号链接：$worker_plist"
grep -Fq "<string>$worker_label</string>" "$worker_plist" \
  || die "worker LaunchAgent 备份的 Label 不是 $worker_label"

sha256_bin="${SHA256_BIN:-}"
if [[ -z "$sha256_bin" ]]; then
  if command -v shasum >/dev/null 2>&1; then
    sha256_bin="$(command -v shasum)"
  elif command -v sha256sum >/dev/null 2>&1; then
    sha256_bin="$(command -v sha256sum)"
  else
    die "未找到 shasum 或 sha256sum"
  fi
fi
case "${sha256_bin:t}" in
  shasum)
    checksum_args=(-a 256 -c "$manifest")
    ;;
  sha256sum)
    checksum_args=(-c "$manifest")
    ;;
  *)
    die "SHA256_BIN 必须是 shasum 或 sha256sum：$sha256_bin"
    ;;
esac
if ! (cd "$backup_dir" && "$sha256_bin" "${checksum_args[@]}" >/dev/null); then
  die "SHA256 manifest 校验失败，未改变 Docker service"
fi

read_single_line() {
  local path="$1"
  local value
  value="$(<"$path")"
  [[ -n "$value" ]] || die "文件为空：$path"
  [[ "$value" != *$'\n'* && "$value" != *$'\r'* ]] || die "文件必须只有一行：$path"
  print -r -- "$value"
}

image_id="$(read_single_line "$image_id_file")"
image_tag="$(read_single_line "$image_tag_file")"
grep -Eq '^sha256:[0-9A-Fa-f]{64}$' <<< "$image_id" || die "Docker image id 格式无效"
case "$image_tag" in
  ""|*[!A-Za-z0-9._/@:-]*) die "Docker image tag 格式无效：$image_tag" ;;
esac

runtime_image_tag="$(awk '$1 == "image:" {print $2; exit}' "$runtime_config")"
[[ -n "$runtime_image_tag" ]] || die "runtime Compose 配置缺少 image"
[[ "$runtime_image_tag" == "$image_tag" ]] || die "runtime image tag 与备份 tag 不一致"

if (( dry_run )); then
  print -r -- "dry-run：SHA256 manifest 已验证"
  print -r -- "dry-run：已验证 Docker image id/tag 与 runtime Compose 配置"
  print -r -- "dry-run：已验证 saved worker LaunchAgent：$worker_plist"
  print -r -- "dry-run：将执行 docker image tag $image_id $image_tag"
  print -r -- "dry-run：将只对 $service_name 执行 Docker Compose 回撤并检查 running"
  print -r -- "dry-run：不会写入目标 runtime 配置 $compose_file"
  print -r -- "dry-run：Docker service running 后将恢复并重启 $worker_label"
  print -r -- "dry-run：不会恢复或覆盖 SQLite、objects、微信数据库、钥匙串或业务记录，也不会自动分析"
  exit 0
fi

docker_bin="${DOCKER_BIN:-docker}"
require_command "$docker_bin"
require_command cp
require_command grep
require_command awk
require_command mv
require_command mkdir
require_command chmod
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
"$plutil_bin" -lint "$worker_plist" >/dev/null
saved_worker_label="$("$plist_buddy" -c "Print :Label" "$worker_plist" 2>/dev/null)" \
  || die "无法读取 worker LaunchAgent 的 Label：$worker_plist"
[[ "$saved_worker_label" == "$worker_label" ]] \
  || die "worker LaunchAgent 的 Label 不匹配：$saved_worker_label"

"$docker_bin" image inspect "$image_id" >/dev/null
staged_runtime_config="$compose_file.rollback.$$"
[[ ! -e "$staged_runtime_config" && ! -L "$staged_runtime_config" ]] || die "临时 runtime 配置已存在，为避免覆盖而停止：$staged_runtime_config"
cp -p "$runtime_config" "$staged_runtime_config"
mv -f "$staged_runtime_config" "$compose_file"
compose_args=(compose --project-directory "$project_root" -f "$compose_file")
"$docker_bin" image tag "$image_id" "$image_tag"
"$docker_bin" "${compose_args[@]}" up -d --no-build --pull never --force-recreate "$service_name"
if ! "$docker_bin" "${compose_args[@]}" ps --status running --services "$service_name" | grep -Fxq "$service_name"; then
  die "$service_name 回撤后未确认 running"
fi

mkdir -p "$launchagent_dir"
target_worker_plist="$launchagent_dir/$worker_label.plist"
staged_worker_plist="$target_worker_plist.rollback.$$"
[[ ! -e "$staged_worker_plist" && ! -L "$staged_worker_plist" ]] || die "临时 worker LaunchAgent 已存在，为避免覆盖而停止：$staged_worker_plist"
cp -p "$worker_plist" "$staged_worker_plist"
"$plutil_bin" -lint "$staged_worker_plist" >/dev/null
mv -f "$staged_worker_plist" "$target_worker_plist"
chmod 600 "$target_worker_plist"

domain="gui/$(id -u)"
worker_service="$domain/$worker_label"
"$launchctl_bin" bootout "$worker_service" >/dev/null 2>&1 || true
"$launchctl_bin" bootstrap "$domain" "$target_worker_plist"
"$launchctl_bin" kickstart -k "$worker_service"
worker_state="$("$launchctl_bin" print "$worker_service")"
if ! print -r -- "$worker_state" | grep -Eq 'state = running|pid = [1-9][0-9]*'; then
  die "$worker_service 已加载但未确认 running"
fi

print -r -- "工作台镜像/runtime 配置已回撤；$service_name 和 $worker_service 正在运行。SQLite、objects、微信数据库和钥匙串均未恢复或修改，也未自动分析。"
