#!/usr/bin/env bash
# Usage: build_engine.sh CLEAN_V73_CHECKOUT NDK_R23C WEIGHTS_NPZ OUTPUT
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
engine=$(realpath "$1")
ndk=$(realpath "$2")
weights=$(realpath "$3")
mkdir -p "$4"
output=$(realpath "$4")
python3 "$here/export_glsl.py" "$weights" "$output/shaders"
python3 "$here/patch_engine.py" "$engine" "$output/shaders"
cmake -S "$engine" -B "$engine/build-sr" \
  -DCMAKE_TOOLCHAIN_FILE="$ndk/build/cmake/android.toolchain.cmake" \
  -DANDROID_ABI=arm64-v8a -DANDROID_PLATFORM=android-24 -DANDROID_STL=c++_static \
  -DCMAKE_BUILD_TYPE=Release -DBUILD_Q4=OFF -DBUILD_PREY=OFF -DBUILD_ETQW=OFF \
  -DBUILD_D3_MOD=OFF -DBUILD_Q4_MOD=OFF -DBASE=OFF -DOPENAL=ON \
  -DOBOE_SOURCE="$engine/Q3E/src/main/jni/deplibs/oboe" \
  -DALSOFT_BACKEND_PIPEWIRE=OFF -DALSOFT_BACKEND_PULSEAUDIO=OFF -DALSOFT_BACKEND_ALSA=OFF
cmake --build "$engine/build-sr" --target doom3 -j8
cp "$engine/build-sr/doom3/neo/libdoom3.so" "$output/libdoom3.so"
"$ndk/toolchains/llvm/prebuilt/linux-x86_64/bin/llvm-strip" --strip-unneeded "$output/libdoom3.so"
cp "$here"/sr_*.cfg "$output/"
cp "$weights" "$output/weights.npz"
cp "$engine/LICENSE" "$output/COPYING"
sha256sum "$output/libdoom3.so" "$output/weights.npz"
