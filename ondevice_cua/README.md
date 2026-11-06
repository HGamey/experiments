# ondevice_cua: a 4B computer-use VLM running on the phone

Benchmarks **Mage-VL-Cowork** (Microsoft, arXiv 2610.40747, "hybrid computer use", 4B, OSWorld-Verified 66.0% as
reported by the authors) running **on the phone CPU** through llama.cpp, one agent step at a time.

## Model and license

- Weights are public: [microsoft/Mage-VL-Cowork](https://huggingface.co/microsoft/Mage-VL-Cowork), Apache-2.0
  (revision `6fbe7f91`). Base: Mage-VL (codec-native ViT) + Qwen3-4B-Instruct-2507 decoder.
- On-phone format: third-party GGUF [Hansda/Mage-VL-Cowork-GGUF](https://huggingface.co/Hansda/Mage-VL-Cowork-GGUF)
  (Apache-2.0, revision `90c433e3`): decoder `Q4_K_M` (2.72 GB) + vision encoder/projector `mmproj Q8_0` (0.35 GB).
  These are quantizations of the real model, not a proxy. The benchmark scores belong to the original BF16 model;
  the quantized model's task accuracy was not measured here.
- Runtime: the Mage-ViT projector is not in upstream llama.cpp yet (PR ggml-org/llama.cpp#26796, unmerged), so
  the pinned fork `ch686/llama.cpp-locateanything-magevl@e07bdc8c` is used, built for arm64-v8a
  (`-march=armv8.7a`, CPU backend, no OpenMP).

## What one step is

The original hybrid system prompt (computer_use + bash tool schema, ~1k tokens) + one screenshot of the live
phone screen + the instruction "Open the Settings app and turn on Wi-Fi." The model answers with reasoning, an
`Action:` line and a `<tool_call>`. Greedy decoding, at most 512 new tokens, context 8192, 8 threads.

Screenshot scales: `1` is the native panel resolution, as the official OSWorld harness feeds raw screenshots
(1920x1080 there, about 2k image tokens). The Mage projector ignores `--image-max-tokens` and only caps at 4 MP,
so `2` and `3` are host-side box downscales (about 1/4 and 1/9 of the image tokens).

Reported per step: vision encode time, image-token prefill tok/s, overall prefill tok/s (all prompt tokens,
excluding the encode), decode tok/s, time to first token, step time (model already loaded: image load + prefill +
encode + decode), peak RSS (VmHWM, includes the mmapped weights) and peak anonymous RSS, whole-device power and
temperatures. Plus a text-only `llama-bench` pp512/tg128 sweep over 4/6/8 threads and a 60 s idle baseline.

Power: the phones stay on USB. Whole-device power = USB input power - battery charge power (sysfs
`power_supply/{usb,battery}`), the same estimate as `gs3d/bench.py`; the table also gives the delta over idle.
It is an estimate, not a rail measurement.

## Run

```sh
pf-node --experiment ext/hgamey/ondevice-cua --device-access write \
  --device <NODE_ID>/<DEVICE_ID> --resource device:<serial> --max-runtime-seconds 7200 \
  --detach experiment ondevice_cua/bench.py <serial> [--scales 3,2,1] [--reps 2] [--threads 8]
python3 ondevice_cua/report.py pf-node-out/<job> [...] > results.md
```

The execution host needs `cmake`, an Android NDK (r28c; `ANDROID_NDK_HOME`, or downloaded on Linux) and
network access to HuggingFace (or `--mirror`) and codeload.github.com. Weights (~3.1 GB) and the build are cached
in `~/.cache/hgamey-ondevice-cua` on the host (`--cache` / `OCUA_CACHE` to change); nothing large goes into git.

Phone state: the script wakes the screen and switches to the launcher for the screenshot, pushes files to
`/data/local/tmp/ocua`, and at the end kills only the pids it started, deletes that directory, brings the
original foreground app back and turns the screen off again if it was off. `restore.json` records before/after.

`node_probe.sh` is a read-only check of an execution host (toolchain, network, disk).
