#!/bin/sh
# host_artifacts.sh — 把随任务参数自动上传的本地文件拷进任务输出目录，
# 让它们成为任务产物、可用 job:<任务ID>:<路径> 引用（给留言/交付附图用）。
#   experiment selfsr/host_artifacts.sh <本地文件1> [本地文件2 ...]   # 文件参数会自动打包上传
set -eu
OUT="${PF_JOB_OUT:-.}"
[ $# -ge 1 ] || { echo "用法: experiment selfsr/host_artifacts.sh <文件> [文件 ...]" >&2; exit 2; }
for f in "$@"; do
  cp -v "$f" "$OUT/"
done
echo "== 输出目录 =="
ls -la "$OUT"
