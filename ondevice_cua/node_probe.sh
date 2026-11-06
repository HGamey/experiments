#!/bin/bash
# Read-only probe of the execution host: toolchain, network, disk. Touches no phone.
set -u
echo "uname: $(uname -a)"
echo "user=$(id -un) home=$HOME pwd=$(pwd)"
echo "PF_ROOT=${PF_ROOT:-} PF_JOB_OUT=${PF_JOB_OUT:-} ANDROID_NDK_HOME=${ANDROID_NDK_HOME:-} ANDROID_HOME=${ANDROID_HOME:-}"
for t in cmake ninja make git curl python3 adb hf unzip; do printf '%s: ' $t; command -v $t || echo missing; done
cmake --version 2>/dev/null | head -1
python3 --version
nproc 2>/dev/null || sysctl -n hw.ncpu
for d in "${ANDROID_NDK_HOME:-}" "$HOME/Library/Android/sdk/ndk" "$HOME/Android/Sdk/ndk" /opt/android-ndk* "${PF_ROOT:-/nonexistent}/../tools/ndk"; do [ -n "$d" ] && [ -e "$d" ] && { echo "ndk candidate: $d"; ls "$d" 2>/dev/null | head; }; done
df -h "$HOME" . 2>/dev/null
env | grep -i -E '^(https?|all|no)_proxy=' | sed 's#//[^@]*@#//***@#'
for u in https://huggingface.co/api/models/Hansda/Mage-VL-Cowork-GGUF https://hf-mirror.com/api/models/Hansda/Mage-VL-Cowork-GGUF https://dl.google.com/android/repository/repository2-1.xml https://github.com; do
  printf '%s -> ' "$u"; curl -s -o /dev/null -m 20 -w '%{http_code} %{time_total}s\n' "$u" || echo fail
done
# Speed test: 50 MB range of the Q4_K_M weights
for h in huggingface.co hf-mirror.com; do
  printf 'dl %s: ' $h; curl -sL -m 60 -r 0-52428799 -o /dev/null -w '%{size_download} B %{speed_download} B/s\n' "https://$h/Hansda/Mage-VL-Cowork-GGUF/resolve/main/Mage-VL-Cowork-Q4_K_M.gguf" || echo fail
done
ls -la "$HOME/.cache" 2>/dev/null | head -20
