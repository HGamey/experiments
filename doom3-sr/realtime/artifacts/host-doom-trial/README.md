# 主机 GLES 三档对比

已用 ea5d3a9 正式包内 SHA256 `6cc44b53ab9d6701617c48a24515a49ac20ca7e7417bcb3c7d5d02570f1d60e5` 的真实模型，运行同包的 C++ 后处理及 GLSL，生成三档图片。输入取自 C 作业 `3aa25dd077c5` 的 `test-crop-contact.jpg`：第一行第三列 `frame_0124 HR`，坐标 `[512,24,768,280)`，256×256；原拼图也保存在本目录。模式 0 原样显示该 HR 图；模式 1/2 用 Pillow bicubic 下采样到 128×128，再分别运行 bicubic / 模型放大。这里没有重新渲染 Doom 场景；原图已经 JPEG 压缩，不能冒充手机端实时画质、完整帧或帧率验收。此裁剪的 RGB PSNR 为 bicubic 37.340 dB、模型 38.767 dB（+1.426 dB），只描述这一张输入。模式 0 和源图逐字节相等，模式 1 和 Pillow 逐字节相等；模式 2 对 NumPy 参照最大误差 0.002278（小于 1/255）。

文件：`mode-0-native.png`、`mode-1-bicubic.png`、`mode-2-model.png` 是三个独立输出；`three-modes.png` 为并排预览；`zoom-three-modes.png` 将三图相同 `[72,40,200,168)` 区域最近邻放大 4 倍。未做额外锐化或调色。`report.json` 记录包、权重、源码和图片哈希及真实 GPU（RTX 4090 / GLES 3.2）。

复现（在 `doom3-sr/realtime` 下，Python 环境同 validate_gles.py）：

```sh
python render_host_comparison.py artifacts/small-sr-doom-arm64.tar.gz \
  artifacts/host-doom-trial/source-contact.jpg /tmp/doom-host-comparison
```
