#!/bin/bash
# build.sh — 编译手机端 rdtool (arm64)。需要 NDK r28c 和 RenderDoc v1.46 的源码头文件与安卓库, 都不进仓库:
#   NDK:       https://dl.google.com/android/repository/android-ndk-r28c-linux.zip (用 unzip 解压)
#   头文件:    git clone --depth 1 --branch v1.46 https://github.com/baldurk/renderdoc.git
#   安卓库:    https://renderdoc.org/stable/1.46/renderdoc_1.46.tar.gz 里
#              share/renderdoc/plugins/android/org.renderdoc.renderdoccmd.arm64.apk 的 lib/arm64-v8a/libVkLayer_GLES_RenderDoc.so
# 用法: NDK=<ndk 目录> RD_SRC=<renderdoc 源码目录> RD_LIB=<libVkLayer_GLES_RenderDoc.so> bash build.sh <输出目录>
# 输出目录里得到 rdtool 和一份同名库 (rdtool 运行时按 rpath $ORIGIN 在自己目录里找库)。
set -euo pipefail
OUT="${1:?输出目录}"
: "${NDK:?}" "${RD_SRC:?}" "${RD_LIB:?}"
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$OUT"
cp "$RD_LIB" "$OUT/libVkLayer_GLES_RenderDoc.so"
"$NDK/toolchains/llvm/prebuilt/linux-x86_64/bin/aarch64-linux-android33-clang++" \
  -std=c++17 -DRENDERDOC_PLATFORM_ANDROID -O2 -rdynamic -static-libstdc++ \
  -I "$RD_SRC/renderdoc/api/replay" "$HERE/rdtool.cpp" "$OUT/libVkLayer_GLES_RenderDoc.so" \
  -Wl,-rpath,'$ORIGIN' -o "$OUT/rdtool"
file "$OUT/rdtool"
