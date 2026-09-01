#!/bin/zsh
set -euo pipefail
setopt null_glob
umask 077

project_root="$(cd "$(dirname "$0")/.." && pwd)"
backup_dir=""
launchagent_dir="${WORKBENCH_LAUNCHAGENT_DIR:-$HOME/Library/LaunchAgents}"
compose_file=""
dry_run=0

usage() {
  print -r -- "用法：zsh deploy/create-wechat-direct-backup.sh [选项]"
  print -r -- "  --backup-dir PATH       备份目录；默认写入 project_root/backups/"
  print -r -- "  --project-root PATH     工作台项目根目录"
  print -r -- "  --launchagent-dir PATH  LaunchAgent 目录"
  print -r -- "  --compose-file PATH     Docker Compose 文件"
  print -r -- "  --dry-run               只显示计划，不读取或写入运行环境"
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
    --launchagent-dir)
      (($# >= 2)) || die "--launchagent-dir 缺少路径"
      launchagent_dir="$2"
      shift 2
      ;;
    --compose-file)
      (($# >= 2)) || die "--compose-file 缺少路径"
      compose_file="$2"
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

if [[ -z "$compose_file" ]]; then
  compose_file="$project_root/compose.yaml"
fi
if [[ -z "$backup_dir" ]]; then
  backup_dir="$project_root/backups/wechat-direct-$(date +%Y%m%d-%H%M%S)"
fi

if [[ ! -d "$project_root" ]]; then
  die "项目根目录不存在：$project_root"
fi

if (( dry_run )); then
  print -r -- "dry-run：将创建备份目录 $backup_dir"
  print -r -- "dry-run：将保存 Git SHA/status/diff、formal SQLite、objects.tgz、LaunchAgent plists、Docker image id/tag 和 SHA256SUMS"
  print -r -- "dry-run：不会调用 Docker、git、launchctl，也不会写入文件"
  exit 0
fi

require_command git
require_command cp
require_command mkdir
require_command awk
require_command chmod
require_command tar
python_bin="${PYTHON_BIN:-python3}"
docker_bin="${DOCKER_BIN:-docker}"
tar_bin="${TAR_BIN:-tar}"
require_command "$python_bin"
require_command "$docker_bin"
require_command "$tar_bin"

[[ -f "$compose_file" ]] || die "未找到 Docker Compose 文件：$compose_file"
git -C "$project_root" rev-parse --show-toplevel >/dev/null 2>&1 || die "不是 Git 项目：$project_root"

worker_plist="$launchagent_dir/com.finance-workbench.worker.plist"
[[ -f "$worker_plist" ]] || die "未找到 worker LaunchAgent：$worker_plist"

image_tag="${WORKBENCH_IMAGE_TAG:-}"
if [[ -z "$image_tag" ]]; then
  image_tag="$(awk '$1 == "image:" {print $2; exit}' "$compose_file")"
fi
case "$image_tag" in
  ""|*[!A-Za-z0-9._/@:-]*) die "Docker image tag 不安全或为空：$image_tag" ;;
esac

database_source="${WORKBENCH_DATABASE_PATH:-}"
objects_source="${WORKBENCH_OBJECTS_PATH:-}"
if [[ -n "$database_source" ]]; then
  [[ -f "$database_source" ]] || die "SQLite 源文件不存在：$database_source"
fi
if [[ -n "$objects_source" ]]; then
  [[ -d "$objects_source" ]] || die "objects 源目录不存在：$objects_source"
fi

if [[ -e "$backup_dir" ]]; then
  die "备份目录已存在，为避免覆盖而停止：$backup_dir"
fi
mkdir -p "$backup_dir/launchagents"

git -C "$project_root" rev-parse HEAD > "$backup_dir/git-head.txt"
git -C "$project_root" status --short --branch > "$backup_dir/git-status.txt"
git -C "$project_root" diff --binary HEAD > "$backup_dir/git-diff.patch"
cp -p "$compose_file" "$backup_dir/compose.yaml"

plist_names=(
  com.finance-workbench.worker.plist
  com.finance-workbench.sync.plist
  com.finance-workbench.server.plist
  com.finance-workbench.wechat-sync.plist
)
for plist_name in "${plist_names[@]}"; do
  source_plist="$launchagent_dir/$plist_name"
  if [[ -f "$source_plist" ]]; then
    cp -p "$source_plist" "$backup_dir/launchagents/$plist_name"
  elif [[ "$plist_name" == "com.finance-workbench.worker.plist" ]]; then
    die "worker LaunchAgent 在备份过程中消失：$source_plist"
  fi
done

image_id="$("$docker_bin" image inspect --format '{{.Id}}' "$image_tag")"
[[ -n "$image_id" ]] || die "Docker 未返回 image id：$image_tag"
case "$image_id" in
  *$'\n'*|*$'\r'*|*[[:space:]]*) die "Docker image id 含有非法空白" ;;
esac
print -r -- "$image_id" > "$backup_dir/docker-image-id.txt"
print -r -- "$image_tag" > "$backup_dir/docker-image-tag.txt"

backup_host_sqlite() {
  "$python_bin" - "$1" "$2" <<'PY'
from pathlib import Path
import sqlite3
import sys
from urllib.parse import quote

source = Path(sys.argv[1]).resolve()
target = Path(sys.argv[2])
with sqlite3.connect(f"file:{quote(str(source))}?mode=ro", uri=True) as source_db:
    with sqlite3.connect(target) as target_db:
        source_db.backup(target_db)
PY
}

if [[ -n "$database_source" ]]; then
  backup_host_sqlite "$database_source" "$backup_dir/workbench.sqlite3"
else
  sqlite_stream=$'import os,sqlite3,sys,tempfile\nfd,path=tempfile.mkstemp(prefix="finance-workbench-backup-",suffix=".sqlite3",dir="/tmp")\nos.close(fd)\ntry:\n source=sqlite3.connect("file:"+sys.argv[1]+"?mode=ro",uri=True)\n target=sqlite3.connect(path)\n source.backup(target)\n target.close()\n source.close()\n sys.stdout.buffer.write(open(path,"rb").read())\nfinally:\n os.unlink(path)'
  "$docker_bin" compose --project-directory "$project_root" -f "$compose_file" exec -T workbench python -c "$sqlite_stream" /data/workbench.sqlite3 > "$backup_dir/workbench.sqlite3"
fi

if [[ -n "$objects_source" ]]; then
  objects_source="${objects_source%/}"
  "$tar_bin" -czf "$backup_dir/objects.tgz" -C "${objects_source:h}" "${objects_source:t}"
else
  "$docker_bin" compose --project-directory "$project_root" -f "$compose_file" exec -T workbench tar -czf - -C /data objects > "$backup_dir/objects.tgz"
fi

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

checksum_files=(
  git-head.txt
  git-status.txt
  git-diff.patch
  compose.yaml
  workbench.sqlite3
  objects.tgz
  docker-image-id.txt
  docker-image-tag.txt
)
for plist_path in "$backup_dir"/launchagents/*.plist; do
  [[ -f "$plist_path" ]] || continue
  checksum_files+=("${plist_path#$backup_dir/}")
done
for checksum_file in "${checksum_files[@]}"; do
  [[ -f "$backup_dir/$checksum_file" ]] || die "备份文件缺失，无法生成 manifest：$checksum_file"
done

case "${sha256_bin:t}" in
  shasum)
    (cd "$backup_dir" && "$sha256_bin" -a 256 "${checksum_files[@]}") > "$backup_dir/SHA256SUMS"
    ;;
  sha256sum)
    (cd "$backup_dir" && "$sha256_bin" "${checksum_files[@]}") > "$backup_dir/SHA256SUMS"
    ;;
  *)
    die "SHA256_BIN 必须是 shasum 或 sha256sum：$sha256_bin"
    ;;
esac

chmod 700 "$backup_dir" "$backup_dir/launchagents"
for backup_file in "$backup_dir"/* "$backup_dir"/launchagents/*; do
  [[ -f "$backup_file" ]] && chmod 600 "$backup_file"
done

print -r -- "备份已创建：$backup_dir"
print -r -- "SQLite 未在任何回撤脚本中自动恢复；本备份仅用于可验证回撤。"
